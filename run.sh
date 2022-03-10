#!/usr/bin/env bash

python main.py --task tasd \
            --dataset rest15 \
            --model_name_or_path ./pretrained-models/t5-base \
            --paradigm annotation \
            --n_gpu 0 \
            --do_eval \
            --train_batch_size 8 \
            --gradient_accumulation_steps 2 \
            --eval_batch_size 16 \
            --learning_rate 3e-4 \
            --num_train_epochs 20 