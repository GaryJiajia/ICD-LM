from typing import Dict, List, Optional, Union

import more_itertools
import torch

from src.load_ds_utils import load_coco_ds, load_vqav2_ds
from src.lvlm_interface import FlamingoInterface, IDEFICSInterface
from src.metrics.cider_calculator import compute_cider
from src.metrics.vqa_metrics import postprocess_vqa_generation


def load_ds(cfg, split=None):
    if cfg.task.task_name == 'caption':
        ds = load_coco_ds(
            name=cfg.dataset.name,
            train_coco_dataset_root=cfg.dataset.train_coco_dataset_root,
            train_coco_annotation_file=cfg.dataset.train_coco_annotation_file,
            val_coco_dataset_root=cfg.dataset.val_coco_dataset_root,
            val_coco_annotation_file=cfg.dataset.val_coco_annotation_file,
            split=split,
        )
    elif cfg.task.task_name == 'vqa':
        ds = load_vqav2_ds(
            version=cfg.dataset.version,
            train_path=cfg.dataset.train_path,
            val_path=cfg.dataset.val_path,
            train_coco_dataset_root=cfg.dataset.train_coco_dataset_root,
            val_coco_dataset_root=cfg.dataset.val_coco_dataset_root,
            split=split,
        )
    else:
        raise ValueError(f'{cfg.task.task_name=} error, should in ["caption", "vqa"]')
    return ds


@torch.inference_mode()
def get_info_score(
    interface: Union[FlamingoInterface, IDEFICSInterface],
    choosed_icd_seq_list: List,
    candidate_set: Dict,
    batch_size: int,
    split_token: Optional[str] = None,
    construct_order='left',
):
    # 1. 计算P(y|x)
    # 1.1 拼接文本输入
    test_lang_x_input = interface.gen_ice_prompt(
        choosed_icd_seq_list[-1], add_image_token=True
    )
    prompts = interface.transfer_prompts(
        choosed_icd_seq_list, is_last_for_generation=False
    )

    x_input = interface.prepare_input(
        prompts, is_last_for_generation=False, add_eos_token=True
    ).to(interface.device)

    icd_mask_prompt = interface.concat_prompt(
        choosed_icd_seq_list[:-1],
        add_eos_token=False,
        add_image_token=True,
        is_last_for_generation=False,
    )
    query_mask_part = test_lang_x_input.split(split_token)[0] + split_token

    mask_context = icd_mask_prompt + query_mask_part

    mask_length = interface.get_input_token_num(mask_context)
    cond_prob = interface.get_cond_prob(x_input, mask_length=[mask_length])

    # 2. 计算P(y|x, c)
    info_score_list = []
    cand_idx = sorted(list(candidate_set.keys()))
    for batch in more_itertools.chunked(cand_idx, batch_size):
        batch_data = [candidate_set[i] for i in batch]

        # 2.1 拼接文本输入
        if construct_order == 'left':
            add_new_icd_seq_list = [
                [new_icd] + choosed_icd_seq_list for new_icd in batch_data
            ]
        elif construct_order == 'right':
            add_new_icd_seq_list = [
                choosed_icd_seq_list[:-1] + [new_icd] + [choosed_icd_seq_list[-1]]
                for new_icd in batch_data
            ]
        else:
            raise ValueError(
                f"the construct_order should be left or right, but got {construct_order}"
            )

        prompts = interface.transfer_prompts(
            add_new_icd_seq_list, is_last_for_generation=False
        )

        add_new_icd_input = interface.prepare_input(
            prompts,
            is_last_for_generation=False,
            add_eos_token=True,
        ).to(interface.device)
        icd_mask_prompt_list = [
            interface.concat_prompt(
                t[:-1],
                add_eos_token=False,
                add_image_token=True,
                is_last_for_generation=False,
            )
            for t in add_new_icd_seq_list
        ]

        mask_context_list = [
            icd_mask_prompt + query_mask_part
            for icd_mask_prompt in icd_mask_prompt_list
        ]

        mask_length_list = [
            interface.get_input_token_num(mask_context)
            for mask_context in mask_context_list
        ]
        new_cond_prob = interface.get_cond_prob(
            add_new_icd_input, mask_length=mask_length_list
        )
        sub_info_score = new_cond_prob - cond_prob
        info_score_list.append(sub_info_score)
    return torch.cat(info_score_list)


@torch.inference_mode()
def get_cider_score(
    interface,
    choosed_icd_seq_list: List,
    candidate_set: Dict,
    batch_size: int,
    model_name: str,
    train_ann_path: str,
    construct_order='left',
    gen_kwargs: Dict = None,
):
    output_dict = {}

    prompts = interface.transfer_prompts(
        choosed_icd_seq_list, is_last_for_generation=True
    )

    x_input = interface.prepare_input(
        prompts, is_last_for_generation=True, add_eos_token=True
    ).to(interface.device)

    origin_outputs = interface.generate(
        **x_input,
        pad_token_id=interface.tokenizer.pad_token_id,
        eos_token_id=interface.tokenizer.eos_token_id,
        **gen_kwargs,
    )

    origin_outputs = origin_outputs.tolist()
    prompt_len = int(x_input['attention_mask'].shape[1])

    generated = interface.tokenizer.batch_decode(
        [output[prompt_len:] for output in origin_outputs],
        skip_special_tokens=True,
    )
    pred_coco = [
        {'image_id': choosed_icd_seq_list[-1]['image_id'], 'caption': generated[0]}
    ]

    origin_cider_score = compute_cider(pred_coco, train_ann_path, reduce_cider=False)
    origin_cider_score = origin_cider_score[choosed_icd_seq_list[-1]['image_id']][
        'CIDEr'
    ]

    cand_idx = sorted(list(candidate_set.keys()))
    for batch in more_itertools.chunked(cand_idx, batch_size):
        batch_data = [candidate_set[i] for i in batch]
        if construct_order == 'left':
            add_new_icd_seq_list = [
                [new_icd] + choosed_icd_seq_list for new_icd in batch_data
            ]
        elif construct_order == 'right':
            add_new_icd_seq_list = [
                choosed_icd_seq_list[:-1] + [new_icd] + [choosed_icd_seq_list[-1]]
                for new_icd in batch_data
            ]
        else:
            raise ValueError(
                f"the construct_order should be left or right, but got {construct_order}"
            )
        prompts = interface.transfer_prompts(
            add_new_icd_seq_list, is_last_for_generation=True
        )
        add_new_icd_input = interface.prepare_input(
            prompts,
            is_last_for_generation=True,
            add_eos_token=True,
        ).to(interface.device)

        outputs = interface.generate(
            **add_new_icd_input,
            pad_token_id=interface.tokenizer.pad_token_id,
            eos_token_id=interface.tokenizer.eos_token_id,
            **gen_kwargs,
        )
        outputs = outputs.tolist()
        prompt_len = int(add_new_icd_input['attention_mask'].shape[1])

        generated = interface.tokenizer.batch_decode(
            [output[prompt_len:] for output in outputs],
            skip_special_tokens=True,
        )
        for i, data in enumerate(batch_data):
            output_dict[data['idx']] = {}
            output_dict[data['idx']]['prediction'] = generated[i]
            output_dict[data['idx']]['image_id'] = data['image_id']

    pred_coco = []
    for idx in output_dict:
        pred_coco.append(
            {
                'image_id': output_dict[idx]['image_id'],
                'caption': caption_postprocess(
                    output_dict[idx]['prediction'], model_name=model_name
                ),
            }
        )
    cider_score_info = compute_cider(pred_coco, train_ann_path, reduce_cider=False)
    cider_score = []
    for idx in cand_idx:
        img_id = candidate_set[idx]['image_id']
        cider_score.append(cider_score_info[img_id]['CIDEr'])

    return torch.tensor(cider_score) - origin_cider_score


def caption_postprocess(text, model_name):
    if 'flamingo' in model_name:
        return text.split("Output", 1)[0].replace('"', "")
    elif 'idefics' in model_name:
        return text.split("Caption", 1)[0].replace('"', "").replace('\n', '')


def vqa_postprocess(text, model_name):
    if 'flamingo' in model_name:
        return postprocess_vqa_generation(text)
    elif 'idefics' in model_name:
        return postprocess_vqa_generation(text).replace('\n', '')
