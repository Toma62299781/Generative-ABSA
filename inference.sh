#!/usr/bin/env bash

python inference.py --task tasd \
            --file_path ./inf.txt \
            --model_name_or_path ./pretrained-models/t5-base \
            --paradigm annotation \
            --n_gpu 0 \
            --train_batch_size 8 \
            --gradient_accumulation_steps 2 \
            --eval_batch_size 16 \
            --learning_rate 3e-4 \
            --ckpt ./outputs/tasd/rest15/annotation/cktepoch=20.ckpt