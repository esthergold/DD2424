"Fine-tuning BertMasked Model with labeled dataset"
from __future__ import absolute_import, division, print_function
import argparse
import logging
import os
import random
import csv

import numpy as np
import torch
from torch.utils.data import DataLoader, RandomSampler, TensorDataset, SequentialSampler
from torch.utils.data.distributed import DistributedSampler
from tqdm import trange
import shutil

from pytorch_pretrained_bert.file_utils import PYTORCH_PRETRAINED_BERT_CACHE
from pytorch_pretrained_bert.modeling import BertForMaskedLM
from pytorch_pretrained_bert.tokenization import BertTokenizer
from pytorch_pretrained_bert.optimization import BertAdam

logger = logging.getLogger(__name__)


class InputExample(object):
    """A single training/test example for simple sequence classification."""

    def __init__(self, guid, text_a, text_b=None, label=None):
        """Constructs a InputExample.

        Args:
            guid: Unique id for the example.
            text_a: string. The untokenized text of the first sequence. For single
            sequence tasks, only this sequence must be specified.
            text_b: (Optional) string. The untokenized text of the second sequence.
            Only must be specified for sequence pair tasks.
            label: (Optional) string. The label of the example. This should be
            specified for train and dev examples, but not for test examples.
        """
        self.guid = guid
        self.text_a = text_a
        self.text_b = text_b
        self.label = label


class InputFeatures(object):
    """A single set of features of data."""

    def __init__(self, init_ids, input_ids, input_mask, segment_ids, masked_lm_labels):
        self.init_ids = init_ids
        self.input_ids = input_ids
        self.input_mask = input_mask
        self.segment_ids = segment_ids
        self.masked_lm_labels = masked_lm_labels


class DataProcessor(object):
    """Base class for data converters for sequence classification data sets."""

    def get_train_examples(self, data_dir):
        """Gets a collection of `InputExample`s for the train set."""
        raise NotImplementedError()

    def get_dev_examples(self, data_dir):
        """Gets a collection of `InputExample`s for the dev set."""
        raise NotImplementedError()

    def get_labels(self):
        """Gets the list of labels for this data set."""
        raise NotImplementedError()

    @classmethod
    def _read_csv(cls, input_file, quotechar='"'):
        """Reads a comma separated value file."""
        with open(input_file,"r",encoding='UTF-8') as f:
            reader = csv.reader(
                f,
                delimiter=",",
                quotechar=quotechar,
                doublequote=True,
                skipinitialspace=False,
                )
            lines = []
            for line in enumerate(reader):
                    lines.append(line)
            # delete label and sentence
            del lines[0]
        return lines


class AugProcessor(DataProcessor):
    """Processor for dataset to be augmented."""

    def get_train_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_csv(os.path.join(data_dir, "train.csv")), "train")

    def get_dev_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_csv(os.path.join(data_dir, "dev.csv")), "dev")

    def get_labels(self, name):
        """add your dataset here"""
        if name in ['toxic']:
            return ["0", "1"]

    def _create_examples(self, lines, set_type):
        """Creates examples for the training and dev sets."""
        examples = []
        for (i, line) in enumerate(lines):
            guid ="%s-%s" % (set_type, i)
            text_a = line[1][0]
            text_b = None
            label = line[1][-1]
            examples.append(
                InputExample(guid, text_a, text_b, label))
        return examples


def convert_examples_to_features(examples, label_list, max_seq_length, tokenizer):
    """Loads a data file into a list of `InputBatch`s."""

    label_map = {label : i for i, label in enumerate(label_list)}

    features = []
    dupe_factor = 5
    masked_lm_prob = 0.15
    rng = random.Random(123)
    max_predictions_per_seq = 20
    a = examples
    for (ex_index, example) in enumerate(examples):
        tokens_a = tokenizer.tokenize(example.text_a)
        tokens_b = None
        if len(tokens_a) > max_seq_length - 2:  # maxlength = [cls]+token_length + [sep]
            tokens_a = tokens_a[:(max_seq_length - 2)]

        tokens = ["[CLS]"] + tokens_a + ["[SEP]"]
        label_id = label_map[example.label]
        segment_ids = [label_id] * len(tokens)
        masked_lm_labels = [-1]*max_seq_length

        cand_indexes = []
        for (i, token) in enumerate(tokens):
            if token == "[CLS]" or token == "[SEP]":
                continue
            cand_indexes.append(i)

        rng.shuffle(cand_indexes)
        len_cand = len(cand_indexes)

        output_tokens = list(tokens)

        num_to_predict = min(max_predictions_per_seq,
                             max(1, int(round(len(tokens) * masked_lm_prob))))

        masked_lms_pos = []
        covered_indexes = set()
        for index in cand_indexes:
            if len(masked_lms_pos) >= num_to_predict:
                break
            if index in covered_indexes:
                continue
            covered_indexes.add(index)

            masked_token = None
            # 80% of the time, replace with [MASK]
            if rng.random() < 0.8:
                masked_token = "[MASK]"
            else:
                # 10% of the time, keep original
                if rng.random() < 0.5:
                    masked_token = tokens[index]
                # 10% of the time, replace with random word
                else:
                    masked_token = tokens[cand_indexes[rng.randint(0, len_cand - 1)]]

            masked_lm_labels[index] = tokenizer.convert_tokens_to_ids([tokens[index]])[0]
            output_tokens[index] = masked_token
            masked_lms_pos.append(index)

        init_ids = tokenizer.convert_tokens_to_ids(tokens)
        input_ids = tokenizer.convert_tokens_to_ids(output_tokens)

        # The mask has 1 for real tokens and 0 for padding tokens. Only real
        # tokens are attended to.
        input_mask = [1] * len(input_ids)

        # Zero-pad up to the sequence length.
        padding = [0] * (max_seq_length - len(input_ids))
        init_ids += padding
        input_ids += padding
        input_mask += padding
        segment_ids += padding

        assert len(init_ids) == max_seq_length
        assert len(input_ids) == max_seq_length
        assert len(input_mask) == max_seq_length
        assert len(segment_ids) == max_seq_length

        if ex_index < 5:
            logger.info("*** Example ***")
            logger.info("guid: %s" % (example.guid))
            logger.info("tokens: %s" % " ".join(
                [str(x) for x in tokens]))
            logger.info("init_ids: %s" % " ".join([str(x) for x in init_ids]))
            logger.info("input_ids: %s" % " ".join([str(x) for x in input_ids]))
            logger.info("input_mask: %s" % " ".join([str(x) for x in input_mask]))
            logger.info(
                "segment_ids: %s" % " ".join([str(x) for x in segment_ids]))
            logger.info("masked_lm_labels: %s" % " ".join([str(x) for x in masked_lm_labels]))

        features.append(
                InputFeatures(init_ids=init_ids,
                              input_ids=input_ids,
                              input_mask=input_mask,
                              segment_ids=segment_ids,
                              masked_lm_labels=masked_lm_labels))
    return features


def remove_wordpiece(str):
    if len(str) > 1:
        for i in range(len(str) - 1, 0, -1):
            if str[i] == '[PAD]':
                str.remove(str[i])
            elif len(str[i]) > 1 and str[i][0] == '#' and str[i][1] == '#':
                str[i - 1] += str[i][2:]
                str.remove(str[i])
    return " ".join(str[1:-1])


def main():
    parser = argparse.ArgumentParser()

    # Required parameters
    parser.add_argument("--data_dir", default="datasets", type=str,
                        help="The input data dir. Should contain the .tsv files (or other data files) for the task.")
    parser.add_argument("--output_dir", default="aug_data", type=str,
                        help="The output dir for augmented dataset")
    parser.add_argument("--bert_model", default="bert-base-uncased", type=str,
                        help="The path of pretrained bert model.")
    parser.add_argument("--task_name",default="toxic",type=str,
                        help="The name of the task to train.")
    parser.add_argument("--max_seq_length", default=128, type=int,
                        help="The maximum total input sequence length after WordPiece tokenization. \n"
                             "Sequences longer than this will be truncated, and sequences shorter \n"
                             "than this will be padded.")
    parser.add_argument("--do_lower_case", default=True, action='store_true',
                        help="Set this flag if you are using an uncased model.")
    parser.add_argument("--train_batch_size", default=32, type=int,
                        help="Total batch size for training.")
    parser.add_argument("--learning_rate", default=5e-5, type=float,
                        help="The initial learning rate for Adam.")
    parser.add_argument("--num_train_epochs", default=3.0, type=float,
                        help="Total number of training epochs to perform.")
    parser.add_argument("--warmup_proportion", default=0.1, type=float,
                        help="Proportion of training to perform linear learning rate warmup for. "
                             "E.g., 0.1 = 10%% of training.")
    parser.add_argument('--seed', type=int, default=42,
                        help="random seed for initialization")

    args = parser.parse_args()
    print(args)
    run_aug(args, save_every_epoch=True)


def run_aug(args, save_every_epoch=False):
    # Augment the dataset with your own choice of Processer
    processors = {
        "toxic": AugProcessor
    }

    task_name = args.task_name
    if task_name not in processors:
        raise ValueError("Task not found: %s" % (task_name))
    args.data_dir = os.path.join(args.data_dir, task_name)
    args.output_dir = os.path.join(args.output_dir, task_name)

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    os.makedirs(args.output_dir, exist_ok=True)
    processor = processors[task_name]()
    label_list = processor.get_labels(task_name)

    tokenizer = BertTokenizer.from_pretrained(args.bert_model, do_lower_case=args.do_lower_case)

    train_examples = None
    num_train_steps = None
    train_examples = processor.get_train_examples(args.data_dir)
    #dev_examples = processor.get_dev_examples(args.data_dir)
    #train_examples.extend(dev_examples)
    num_train_steps = int(len(train_examples) / args.train_batch_size * args.num_train_epochs)

    # Load fine-tuned model
    def load_model(model_name):
        weights_path = os.path.join(PYTORCH_PRETRAINED_BERT_CACHE,model_name)
        model = torch.load(weights_path)
        return model

    MODEL_name = "{}/BertForMaskedLM_{}_epoch_10".format(task_name.lower(), task_name.lower())
    model = load_model(MODEL_name)
    model.cuda()

    # Prepare optimizer
    param_optimizer = list(model.named_parameters())
    no_decay = ['bias', 'gamma', 'beta']
    optimizer_grouped_parameters = [
        {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)], 'weight_decay_rate': 0.01},
        {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay)], 'weight_decay_rate': 0.0}
    ]
    t_total = num_train_steps
    optimizer = BertAdam(optimizer_grouped_parameters, lr=args.learning_rate,
                         warmup=args.warmup_proportion, t_total=t_total)

    global_step = 0
    train_features = convert_examples_to_features(
        train_examples, label_list, args.max_seq_length, tokenizer)

    logger.info("***** Running training *****")
    logger.info("  Num examples = %d", len(train_examples))
    logger.info("  Batch size = %d", args.train_batch_size)
    logger.info("  Num steps = %d", num_train_steps)
    all_init_ids = torch.tensor([f.init_ids for f in train_features], dtype=torch.long)
    all_input_ids = torch.tensor([f.input_ids for f in train_features], dtype=torch.long)
    all_input_mask = torch.tensor([f.input_mask for f in train_features], dtype=torch.long)
    all_segment_ids = torch.tensor([f.segment_ids for f in train_features], dtype=torch.long)
    all_masked_lm_labels = torch.tensor([f.masked_lm_labels for f in train_features], dtype=torch.long)
    train_data = TensorDataset(all_init_ids, all_input_ids, all_input_mask, all_segment_ids, all_masked_lm_labels)
    print(train_data)
    train_sampler = RandomSampler(train_data)
    train_dataloader = DataLoader(train_data, sampler=train_sampler, batch_size=args.train_batch_size)

    save_model_dir = os.path.join(PYTORCH_PRETRAINED_BERT_CACHE, task_name)
    if not os.path.exists(save_model_dir):
        os.mkdir(save_model_dir)

    MASK_id = tokenizer.convert_tokens_to_ids(['[MASK]'])[0]
    origin_train_path = os.path.join(args.output_dir, "train_origin.csv")
    save_train_path = os.path.join(args.output_dir, "train.csv")
    shutil.copy(origin_train_path, save_train_path)

    for e in trange(int(args.num_train_epochs), desc="Epoch"):
        '''
        avg_loss = 0
        for step, batch in enumerate(train_dataloader):
            model.train()
            batch = tuple(t.cuda() for t in batch)
            _, input_ids, input_mask, segment_ids, masked_ids = batch
            loss = model(input_ids, segment_ids, input_mask, masked_ids)
            loss.backward()
            avg_loss += loss.item()
            optimizer.step()
            model.zero_grad()
            if (step + 1) % 50 == 0:
                print("avg_loss: {}".format(avg_loss / 50))
                avg_loss = 0
        '''
        #torch.cuda.empty_cache()
        shutil.copy(origin_train_path, save_train_path)
        save_train_file = open(save_train_path, 'a', encoding='UTF-8')
        csv_writer = csv.writer(save_train_file, delimiter=',')
        for step, batch in enumerate(train_dataloader):
            model.eval()
            batch = tuple(t.cuda() for t in batch)
            init_ids, _, input_mask, segment_ids, masked_ids = batch
            input_lens = [sum(mask).item() for mask in input_mask]
            masked_idx = np.squeeze([np.random.randint(0, l, max(l//7, 2)) for l in input_lens])
            for ids, idx in zip(init_ids, masked_idx):
                ids[idx] = MASK_id
            predictions = model(init_ids, segment_ids, input_mask)
            print(step)
            for ids, idx, preds, seg in zip(init_ids, masked_idx, predictions, segment_ids):

                pred = torch.argsort(preds)[:,-1][idx]
                ids[idx] = pred
                pred_str = tokenizer.convert_ids_to_tokens(ids.cpu().numpy())
                pred_str = remove_wordpiece(pred_str)
                csv_writer.writerow([pred_str, seg[0].item()])

                pred = torch.argsort(preds)[:,-2][idx]
                ids[idx] = pred
                pred_str = tokenizer.convert_ids_to_tokens(ids.cpu().numpy())
                pred_str = remove_wordpiece(pred_str)
                csv_writer.writerow([pred_str, seg[0].item()])
            #torch.cuda.empty_cache()
        
        predctions = predictions.detach().cpu()
        #torch.cuda.empty_cache()
        bak_train_path = os.path.join(args.output_dir, "train_epoch_{}.csv".format(e))
        shutil.copy(save_train_path, bak_train_path)

        if save_every_epoch:
            save_model_name = "BertForMaskedLM_" + task_name + "_epoch_" + str(e + 1)
            save_model_path = os.path.join(save_model_dir, save_model_name)
            torch.save(model, save_model_path)
        else:
            if (e + 1) % 10 == 0:
                save_model_name = "BertForMaskedLM_aug" + task_name + "_epoch_" + str(e + 1)
                save_model_path = os.path.join(save_model_dir, save_model_name)
                torch.save(model, save_model_path)


if __name__ == "__main__":
    main()
