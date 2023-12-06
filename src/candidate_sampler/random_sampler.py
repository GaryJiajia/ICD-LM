import random

from .base_sampler import BaseSampler


class RandSampler(BaseSampler):
    def __init__(self, candidate_num, sampler_name, dataset_name, cache_dir, overwrite):
        super().__init__(candidate_num=candidate_num,
                         dataset_name=dataset_name,
                         sampler_name=sampler_name,
                         cache_dir=cache_dir,
                         overwrite=overwrite)
        
    def sample(self, anchor_set, train_ds):
        candidate_set_idx = {}
        for s_idx in anchor_set:
            random_candidate_set = random.sample(
                range(0, len(train_ds)), self.candidate_num
            )
            while s_idx in random_candidate_set:
                random_candidate_set = random.sample(
                    list(range(0, len(train_ds))), self.candidate_num
                )
            candidate_set_idx[s_idx] = random_candidate_set
        return candidate_set_idx