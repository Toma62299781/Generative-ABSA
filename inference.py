import argparse
import os
import logging
import time
import pickle
from tqdm import tqdm

import torch
from torch.utils.data import DataLoader
import pytorch_lightning as pl
from pytorch_lightning import data_loader, seed_everything

from transformers import AdamW, T5ForConditionalGeneration, T5Tokenizer
from transformers import get_linear_schedule_with_warmup

from data_utils import ABSADataset, MyDataset
from data_utils import write_results_to_log, read_line_examples_from_file
from eval_utils import compute_scores


logger = logging.getLogger(__name__)


def init_args():
    parser = argparse.ArgumentParser()
    # basic settings
    parser.add_argument(
        "--task",
        default="uabsa",
        type=str,
        required=True,
        help="The name of the task, selected from: [uabsa, aste, tasd, aope]",
    )
    parser.add_argument(
        "--file_path",
        default="rest14",
        type=str,
        required=True,
        help="file path of the text for inference",
    )
    parser.add_argument(
        "--model_name_or_path",
        default="t5-base",
        type=str,
        help="Path to pre-trained model or shortcut name",
    )
    parser.add_argument(
        "--paradigm",
        default="annotation",
        type=str,
        required=True,
        help="The way to construct target sentence, selected from: [annotation, extraction]",
    )
    parser.add_argument(
        "--do_train", action="store_true", help="Whether to run training."
    )
    parser.add_argument(
        "--do_eval",
        action="store_true",
        help="Whether to run eval on the dev/test set.",
    )
    parser.add_argument(
        "--do_direct_eval",
        action="store_true",
        help="Whether to run direct eval on the dev/test set.",
    )

    # Other parameters
    parser.add_argument("--max_seq_length", default=128, type=int)
    parser.add_argument("--n_gpu", default=0)
    parser.add_argument(
        "--train_batch_size",
        default=16,
        type=int,
        help="Batch size per GPU/CPU for training.",
    )
    parser.add_argument(
        "--eval_batch_size",
        default=16,
        type=int,
        help="Batch size per GPU/CPU for evaluation.",
    )
    parser.add_argument(
        "--gradient_accumulation_steps",
        type=int,
        default=1,
        help="Number of updates steps to accumulate before performing a backward/update pass.",
    )
    parser.add_argument("--learning_rate", default=3e-4, type=float)
    parser.add_argument(
        "--num_train_epochs",
        default=20,
        type=int,
        help="Total number of training epochs to perform.",
    )
    parser.add_argument(
        "--seed", type=int, default=42, help="random seed for initialization"
    )

    # training details
    parser.add_argument("--weight_decay", default=0.0, type=float)
    parser.add_argument("--adam_epsilon", default=1e-8, type=float)
    parser.add_argument("--warmup_steps", default=0.0, type=float)
    parser.add_argument(
        "--ckpt",
        default="outputs/tasd/rest15/annotation/cktepoch=18.ckpt",
        type=str,
        required=True,
    )

    args = parser.parse_args()

    return args


def get_dataset(tokenizer, type_path, args):
    return ABSADataset(
        tokenizer=tokenizer,
        data_dir=args.dataset,
        data_type=type_path,
        paradigm=args.paradigm,
        task=args.task,
        max_len=args.max_seq_length,
    )


class T5FineTuner(pl.LightningModule):
    def __init__(self, hparams):
        super(T5FineTuner, self).__init__()
        self.save_hyperparameters(hparams)

        self.model = T5ForConditionalGeneration.from_pretrained(
            hparams.model_name_or_path
        )
        self.tokenizer = T5Tokenizer.from_pretrained(hparams.model_name_or_path)

    def is_logger(self):
        return True

    def forward(
        self,
        input_ids,
        attention_mask=None,
        decoder_input_ids=None,
        decoder_attention_mask=None,
        labels=None,
    ):
        return self.model(
            input_ids,
            attention_mask=attention_mask,
            decoder_input_ids=decoder_input_ids,
            decoder_attention_mask=decoder_attention_mask,
            labels=labels,
        )

    def _step(self, batch):
        lm_labels = batch["target_ids"]
        lm_labels[lm_labels[:, :] == self.tokenizer.pad_token_id] = -100

        outputs = self(
            input_ids=batch["source_ids"],
            attention_mask=batch["source_mask"],
            labels=lm_labels,
            decoder_attention_mask=batch["target_mask"],
        )

        loss = outputs[0]
        return loss

    def training_step(self, batch, batch_idx):
        loss = self._step(batch)

        tensorboard_logs = {"train_loss": loss}
        return {"loss": loss, "log": tensorboard_logs}

    def training_epoch_end(self, outputs):
        avg_train_loss = torch.stack([x["loss"] for x in outputs]).mean()
        tensorboard_logs = {"avg_train_loss": avg_train_loss}
        return {
            "avg_train_loss": avg_train_loss,
            "log": tensorboard_logs,
            "progress_bar": tensorboard_logs,
        }

    def validation_step(self, batch, batch_idx):
        loss = self._step(batch)
        return {"val_loss": loss}

    def validation_epoch_end(self, outputs):
        avg_loss = torch.stack([x["val_loss"] for x in outputs]).mean()
        tensorboard_logs = {"val_loss": avg_loss}
        return {
            "avg_val_loss": avg_loss,
            "log": tensorboard_logs,
            "progress_bar": tensorboard_logs,
        }

    def configure_optimizers(self):
        """Prepare optimizer and schedule (linear warmup and decay)"""
        model = self.model
        no_decay = ["bias", "LayerNorm.weight"]
        optimizer_grouped_parameters = [
            {
                "params": [
                    p
                    for n, p in model.named_parameters()
                    if not any(nd in n for nd in no_decay)
                ],
                "weight_decay": self.hparams.weight_decay,
            },
            {
                "params": [
                    p
                    for n, p in model.named_parameters()
                    if any(nd in n for nd in no_decay)
                ],
                "weight_decay": 0.0,
            },
        ]
        optimizer = AdamW(
            optimizer_grouped_parameters,
            lr=self.hparams.learning_rate,
            eps=self.hparams.adam_epsilon,
        )
        self.opt = optimizer
        return [optimizer]

    def optimizer_step(
        self, epoch, batch_idx, optimizer, optimizer_idx, second_order_closure=None
    ):
        if self.trainer.use_tpu:
            xm.optimizer_step(optimizer)
        else:
            optimizer.step()
        optimizer.zero_grad()
        self.lr_scheduler.step()

    def get_tqdm_dict(self):
        tqdm_dict = {
            "loss": "{:.4f}".format(self.trainer.avg_loss),
            "lr": self.lr_scheduler.get_last_lr()[-1],
        }
        return tqdm_dict

    def train_dataloader(self):
        train_dataset = get_dataset(
            tokenizer=self.tokenizer, type_path="train", args=self.hparams
        )
        dataloader = DataLoader(
            train_dataset,
            batch_size=self.hparams.train_batch_size,
            drop_last=True,
            shuffle=True,
            num_workers=4,
        )
        t_total = (
            (
                len(dataloader.dataset)
                // (self.hparams.train_batch_size * max(1, len(self.hparams.n_gpu)))
            )
            // self.hparams.gradient_accumulation_steps
            * float(self.hparams.num_train_epochs)
        )
        scheduler = get_linear_schedule_with_warmup(
            self.opt,
            num_warmup_steps=self.hparams.warmup_steps,
            num_training_steps=t_total,
        )
        self.lr_scheduler = scheduler
        return dataloader

    def val_dataloader(self):
        val_dataset = get_dataset(
            tokenizer=self.tokenizer, type_path="dev", args=self.hparams
        )
        return DataLoader(
            val_dataset, batch_size=self.hparams.eval_batch_size, num_workers=4
        )


class LoggingCallback(pl.Callback):
    def on_validation_end(self, trainer, pl_module):
        logger.info("***** Validation results *****")
        if pl_module.is_logger():
            metrics = trainer.callback_metrics
        # Log results
        for key in sorted(metrics):
            if key not in ["log", "progress_bar"]:
                logger.info("{} = {}\n".format(key, str(metrics[key])))

    def on_test_end(self, trainer, pl_module):
        logger.info("***** Test results *****")

        if pl_module.is_logger():
            metrics = trainer.callback_metrics

        # Log and save results to file
        output_test_results_file = os.path.join(
            pl_module.hparams.output_dir, "test_results.txt"
        )
        with open(output_test_results_file, "w") as writer:
            for key in sorted(metrics):
                if key not in ["log", "progress_bar"]:
                    logger.info("{} = {}\n".format(key, str(metrics[key])))
                    writer.write("{} = {}\n".format(key, str(metrics[key])))


if __name__ == "__main__":
    # initialization
    args = init_args()

    seed_everything(args.seed)

    tokenizer = T5Tokenizer.from_pretrained(args.model_name_or_path)

    # show one sample to check the sanity of the code and the expected output
    # print(f"Here is an example (from dev set) under `{args.paradigm}` paradigm:")
    # dataset = ABSADataset(tokenizer=tokenizer, data_dir=args.dataset, data_type='dev',
    #                     paradigm=args.paradigm, task=args.task, max_len=args.max_seq_length)
    # data_sample = dataset[2]  # a random data sample
    # print('Input :', tokenizer.decode(data_sample['source_ids'], skip_special_tokens=True))
    # print('Output:', tokenizer.decode(data_sample['target_ids'], skip_special_tokens=True))
    input_data = MyDataset(
        tokenizer=tokenizer,
        file_path=args.file_path,
        paradigm=args.paradigm,
        task=args.task,
        max_len=args.max_seq_length,
    )
    data_loader = DataLoader(input_data, batch_size=32, num_workers=4)

    print(f"\nLoad the trained model from {args.ckpt}...")
    model_ckpt = torch.load(args.ckpt)
    model = T5FineTuner(model_ckpt["hyper_parameters"])
    model.load_state_dict(model_ckpt["state_dict"])

    device = torch.device(f"cuda:{args.n_gpu}")
    model.model.to(device)
    model.model.eval()

    outputs = []
    for batch in tqdm(data_loader):
        outs = model.model.generate(
            input_ids=batch["source_ids"].to(device),
            attention_mask=batch["source_mask"].to(device),
            max_length=128,
        )
        dec = [tokenizer.decode(ids, skip_special_tokens=True) for ids in outs]

        with open("inference_results.txt", "a+", encoding="UTF-8") as fp:
            for d in dec:
                fp.write(d + "\n")
