import os
import os.path
from contextlib import suppress
from typing import Dict, List

import faiss
import more_itertools
import numpy as np
import torch
from huggingface_hub import hf_hub_download
from PIL import Image
from pycocotools.coco import COCO
from torch import nn
from torch.utils.data import Dataset
from torchvision.datasets import VisionDataset
from tqdm import tqdm
from transformers import (
    AutoProcessor,
    AutoTokenizer,
    CLIPTextModelWithProjection,
    CLIPVisionModelWithProjection,
    GPT2LMHeadModel,
)

from datasets import load_dataset
from open_flamingo import create_model_and_transforms
from src.datasets import CocoDataset


def cast_type(precision):
    precision_list = ['fp16', 'bf16', 'fp32']
    if precision == 'fp16':
        return torch.float16
    elif precision == 'bf16':
        return torch.bfloat16
    elif precision == 'fp32':
        return torch.float32
    else:
        raise ValueError(
            f'the precision should in {precision_list}, but got {precision}'
        )


def get_autocast(precision):
    if precision == "fp16":
        return torch.cuda.amp.autocast(dtype=torch.float16)
    elif precision == "bf16":
        # amp_bfloat16 is more stable than amp float16 for clip training
        return lambda: torch.cuda.amp.autocast(dtype=torch.bfloat16)
    else:
        return suppress


def init_flamingo(
    lang_encoder_path,
    tokenizer_path,
    flamingo_checkpoint_path,
    cross_attn_every_n_layers,
    hf_root,
    precision,
    device,
):
    model, image_processor, tokenizer = create_model_and_transforms(
        clip_vision_encoder_path="ViT-L-14",
        clip_vision_encoder_pretrained="openai",
        lang_encoder_path=lang_encoder_path,
        tokenizer_path=tokenizer_path,
        cross_attn_every_n_layers=cross_attn_every_n_layers,
    )
    hf_root = 'openflamingo/' + hf_root
    flamingo_checkpoint_path = hf_hub_download(
        hf_root, "checkpoint.pt", local_dir=flamingo_checkpoint_path
    )
    model.load_state_dict(torch.load(flamingo_checkpoint_path), strict=False)
    data_type = cast_type(precision)
    model.to(device=device, dtype=data_type, non_blocking=True)
    model.eval()
    tokenizer.padding_side = 'left'
    autocast_context = get_autocast(precision)
    return model, image_processor, tokenizer, autocast_context


@torch.inference_mode()
def encode_dataset(
    coco_dataset,
    pool_method,
    dataset_embedding_cache_path,
    batch_size,
    lang_encoder_path,
    tokenizer_path,
    flamingo_checkpoint_path,
    cross_attn_every_n_layers,
    hf_root,
    precision,
    device,
):
    if os.path.exists(dataset_embedding_cache_path):
        dataset_embeddings_map = torch.load(dataset_embedding_cache_path)
        return dataset_embeddings_map

    # load flamingo
    flamingo, image_processor, tokenizer, autocast_context = init_flamingo(
        lang_encoder_path,
        tokenizer_path,
        flamingo_checkpoint_path,
        cross_attn_every_n_layers,
        hf_root,
        precision,
        device,
    )

    if pool_method == 'first':
        tokenizer.padding_side = 'right'
    elif pool_method == 'last':
        tokenizer.padding_side = 'left'

    dataset_embeddings_map = {}
    with autocast_context():
        for batch_data in more_itertools.chunked(tqdm(coco_dataset), batch_size):
            lang_x = [d['caption'] for d in batch_data]
            lang_x_input = tokenizer(lang_x, return_tensors='pt', padding=True).to(
                device
            )

            image_input = [Image.open(d['image']).convert('RGB') for d in batch_data]
            vision_x = torch.stack(
                [image_processor(image) for image in image_input], dim=0
            )
            vision_x = vision_x.unsqueeze(dim=1).unsqueeze(dim=1).to(device)

            features = flamingo(
                vision_x=vision_x,
                lang_x=lang_x_input['input_ids'],
                attention_mask=lang_x_input['attention_mask'],
                output_hidden_states=True,
            ).hidden_states[-1]
            if pool_method == 'last':
                features = features[:, -1, :].detach().cpu().float()
            elif pool_method == 'mean':
                mask = (lang_x_input['attention_mask'] != 0).float()
                masked_features = features * mask.unsqueeze(-1)
                sum_features = masked_features.sum(dim=1)
                count = mask.sum(dim=1, keepdim=True)
                mean_features = sum_features / count
                features = mean_features.detach().cpu().float()
            elif pool_method == 'first':
                features = features[:, 0, :].detach().cpu().float()
            else:
                raise ValueError(f'the pool_method got {pool_method}')
            idx_list = [d['idx'] for d in batch_data]
            for i, idx in enumerate(idx_list):
                dataset_embeddings_map[idx] = features[i]

    torch.save(dataset_embeddings_map, dataset_embedding_cache_path)
    return dataset_embeddings_map


def recall_sim_feature(test_vec, train_vec, top_k=200):
    print(f'embedding shape: {train_vec.shape}')
    # train_vec = train_vec.astype(np.float32)
    # faiss.normalize_L2(train_vec)
    dim = train_vec.shape[-1]  # 向量维度
    index_feat = faiss.IndexFlatIP(dim)
    index_feat.add(train_vec)

    # test_vec = test_vec.astype(np.float32)
    # faiss.normalize_L2(test_vec)
    dist, index = index_feat.search(test_vec, top_k)
    return dist, index


@torch.inference_mode()
def encode_text(
    text_list, device, model_type='openai/clip-vit-large-patch14', batch_size=128
):
    model = CLIPTextModelWithProjection.from_pretrained(model_type).to(device)
    model.eval()
    tokenizer = AutoTokenizer.from_pretrained(model_type)
    final_text_feature = []

    for batch in more_itertools.chunked(tqdm(text_list), batch_size):
        inputs = tokenizer(batch, padding=True, return_tensors="pt").to(device)
        text_feature = model(**inputs).text_embeds
        text_feature /= text_feature.norm(dim=-1, keepdim=True)
        final_text_feature.append(text_feature)

    final_text_feature = torch.cat(final_text_feature, dim=0)
    return final_text_feature.detach().cpu().numpy()


@torch.inference_mode()
def encode_image(
    image_list, device, model_type='openai/clip-vit-large-patch14', batch_size=128
):
    model = CLIPVisionModelWithProjection.from_pretrained(model_type).to(device)
    processor = AutoProcessor.from_pretrained(model_type)
    model.eval()

    final_image_feature = []
    for batch in more_itertools.chunked(tqdm(image_list), batch_size):
        images = [Image.open(image).convert('RGB') for image in batch]
        inputs = processor(images=images, return_tensors="pt").to(device)
        image_feature = model(**inputs).image_embeds
        image_feature /= image_feature.norm(dim=-1, keepdim=True)
        final_image_feature.append(image_feature)

    final_image_feature = torch.cat(final_image_feature, dim=0)
    return final_image_feature.detach().cpu().numpy()


def load_coco_train_ds(cfg):
    if cfg.use_karpathy_split:
        assert '2014' in cfg.dataset.train_coco_dataset_root
        train_ds = load_dataset("yerevann/coco-karpathy", split='train')
        train_ds = train_ds.sort("cocoid")
        ds = ds.rename_columns({'sentences': 'captions', 'cocoid': 'image_id'})

        def transform(example, idx):
            example['single_caption'] = example['captions'][0]
            example['image'] = os.path.join(
                cfg.dataset.train_coco_dataset_root, example['filename']
            )
            example['idx'] = idx
            return example

        train_ds = train_ds.map(
            transform,
            with_indices=True,
            remove_columns=['sentids', 'imgid', 'url', 'filename', 'split'],
        )

    else:
        train_ds = CocoDataset(
            cfg.dataset.train_coco_dataset_root, cfg.dataset.train_coco_annotation_file
        )
    return train_ds


def data_split(generated_data, train_ratio):
    # 获得有多少条test数据
    test_dataset_id_set = {
        v[-1] for d in generated_data for v in generated_data[d]['id_list']
    }
    test_dataset_len = len(test_dataset_id_set)

    # 计算多少test数据用于训练 剩下部分用于监督val loss
    train_data_len = int(train_ratio * test_dataset_len)
    train_idx_set = set(sorted(list(test_dataset_id_set))[:train_data_len])
    val_idx_set = test_dataset_id_set - train_idx_set

    train_data_list = list()
    val_data_list = list()
    for d in generated_data:
        for i in generated_data[d]['id_list']:
            if int(i[-1]) in train_idx_set:
                train_data_list.append(i)
            elif int(i[-1]) in val_idx_set:
                val_data_list.append(i)
            else:
                raise ValueError()

    print(f'the train size {len(train_data_list)}, the test size {len(val_data_list)}')
    return train_data_list, val_data_list


def collate_fn(batch):
    bs = len(batch)
    sample_data = batch[0]
    if not isinstance(sample_data['ice_input'], torch.Tensor):
        ice_num = batch[0]['ice_input'].input_ids.size(0)
        ice_input_ids = [item['ice_input']['input_ids'] for item in batch]
        ice_attn_masks = [item['ice_input']['attention_mask'] for item in batch]

        # padding ice text
        ice_max_len = max([i.size(1) for i in ice_input_ids])
        padded_ice_input_ids = torch.zeros(
            (bs, ice_num, ice_max_len), dtype=ice_input_ids[0].dtype
        )
        padded_ice_attn_masks = torch.zeros(
            (bs, ice_num, ice_max_len), dtype=ice_input_ids[0].dtype
        )
        for i in range(bs):
            seq_len = ice_input_ids[i].size(1)
            padded_ice_input_ids[i, :, :seq_len] = ice_input_ids[i]
            padded_ice_attn_masks[i, :, :seq_len] = ice_attn_masks[i]

        return {
            'ice_input': {
                'input_ids': padded_ice_input_ids,
                'attention_mask': padded_ice_attn_masks,
            },
            'img_input': torch.cat([item['img_input'] for item in batch], dim=0),
            'ice_seq_idx': torch.stack([item['ice_seq_idx'] for item in batch]),
        }
    else:
        return {
            'img_input': torch.cat([item['img_input'] for item in batch], dim=0),
            'ice_input': torch.stack([item['ice_seq_idx'] for item in batch]),
            'ice_seq_idx': torch.stack([item['ice_seq_idx'] for item in batch]),
        }
