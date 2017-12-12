import argparse
import multiprocessing
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim

from hooks import *
from torch.optim.lr_scheduler import StepLR
from torch.utils.data import DataLoader
from torchtext import data
from torchtext import datasets
from ignite.trainer import Trainer, TrainingEvents
from ignite.handlers.logging import log_training_simple_moving_average
from ignite.handlers.logging import log_validation_simple_moving_average

from modules.transformer import Transformer


def run(model_dir, enc_max_vocab, dec_max_vocab, encoder_emb_size,
        decoder_emb_size, encoder_units, decoder_units, batch_size, epochs,
        decay_step, decay_percent, log_interval, save_interval,
        compare_interval):
    source = data.Field(batch_first=True)
    target = data.Field(batch_first=True)

    train, val, _ = datasets.WMT14.splits(
        exts=(".de", ".en"),
        root="./",
        fields=(target, source),
        train="train",
        validation="eval",
        test="test")

    target.build_vocab(train.trg, min_freq=3, max_size=dec_max_vocab)
    source.build_vocab(train.src, min_freq=3, max_size=enc_max_vocab)

    encoder_vocab_size = len(source.vocab.freqs)
    decoder_vocab_size = len(target.vocab.freqs)

    transformer = Transformer(
        max_length=100,
        enc_vocab_size=encoder_vocab_size,
        dec_vocab_size=decoder_vocab_size,
        enc_emb_size=encoder_emb_size,
        dec_emb_size=decoder_emb_size,
        enc_units=encoder_units,
        dec_units=decoder_units)
    loss_fn = nn.CrossEntropyLoss()
    opt = optim.Adam(transformer.parameters())
    lr_decay = StepLR(opt, step_size=decay_step, gamma=decay_percent)

    if torch.cuda.is_available():
        device_data = 0
        transformer.cuda()
        loss_fn.cuda()
    else:
        device_data = -1

    train_iter, val_iter = data.BucketIterator.splits(
        (train, val),
        batch_size=batch_size,
        repeat=False,
        shuffle=True,
        device=device_data)

    def training_update_function(batch):
        transformer.train()
        lr_decay.step()
        opt.zero_grad()

        softmaxed_predictions, predictions = transformer(batch.src, batch.trg)

        flattened_predictions = predictions.view(-1, decoder_units[-1])
        flattened_target = batch.trg.view(-1)

        loss = loss_fn(flattened_predictions, flattened_target)

        loss.backward()
        opt.step()

        return softmaxed_predictions, loss.data[0]

    def validation_inference_function(batch):
        transformer.eval()
        softmaxed_predictions, predictions = transformer(batch.src, batch.trg)

        flattened_predictions = predictions.view(-1, decoder_units[-1])
        flattened_target = batch.trg.view(-1)

        loss = loss_fn(flattened_predictions, flattened_target)

        return loss.data[0]

    trainer = Trainer(train_iter, training_update_function, val_iter,
                      validation_inference_function)
    trainer.add_event_handler(TrainingEvents.TRAINING_STARTED,
                              restore_checkpoint_hook(transformer, model_dir))
    trainer.add_event_handler(
        TrainingEvents.TRAINING_ITERATION_COMPLETED,
        log_training_simple_moving_average,
        window_size=10,
        metric_name="CrossEntropy",
        should_log=
        lambda trainer: trainer.current_iteration % log_interval == 0,
        history_transform=lambda history: history[-1])
    trainer.add_event_handler(
        TrainingEvents.TRAINING_ITERATION_COMPLETED,
        save_checkpoint_hook(transformer, model_dir),
        should_save=
        lambda trainer: trainer.current_iteration % save_interval == 0)
    trainer.add_event_handler(
        TrainingEvents.TRAINING_ITERATION_COMPLETED,
        print_current_prediction_hook(source.vocab),
        should_print=
        lambda trainer: trainer.current_iteration % compare_interval == 0)
    trainer.add_event_handler(
        TrainingEvents.VALIDATION_COMPLETED,
        log_validation_simple_moving_average,
        window_size=10,
        metric_name="CrossEntropy")
    trainer.add_event_handler(
        TrainingEvents.TRAINING_COMPLETED,
        save_checkpoint_hook(transformer, model_dir),
        should_save=lambda trainer: True)
    trainer.run(max_epochs=epochs, validate_every_epoch=True)


if __name__ == '__main__':
    PARSER = argparse.ArgumentParser(
        description="Google's transformer implementation in PyTorch")
    PARSER.add_argument(
        "--batch_size",
        type=int,
        default=2,
        help="Number of batch in single iteration")
    PARSER.add_argument(
        "--epochs", type=int, default=10000, help="Number of epochs")
    PARSER.add_argument(
        "--enc_max_vocab",
        type=int,
        default=80000,
        help="Maximum vocabs for encoder")
    PARSER.add_argument(
        "--dec_max_vocab",
        type=int,
        default=80000,
        help="Maximum vocabs for decoder")
    PARSER.add_argument(
        "--encoder_units",
        default="512,512,512,512,512,512",
        help="Number of encoder units for every layers. Separable by commas")
    PARSER.add_argument(
        "--decoder_units",
        default="512,512,512,512,512,512",
        help="Number of decoder units for every layers. Separable by commas")
    PARSER.add_argument(
        "--encoder_emb_size",
        type=int,
        default=512,
        help="Size of encoder's embedding")
    PARSER.add_argument(
        "--decoder_emb_size",
        type=int,
        default=512,
        help="Size of decoder's embedding")
    PARSER.add_argument(
        "--log_interval",
        type=int,
        default=2,
        help="""Print loss for every N steps""")
    PARSER.add_argument(
        "--save_interval",
        type=int,
        default=10,
        help="""Save model for every N steps""")
    PARSER.add_argument(
        "--compare_interval",
        type=int,
        default=10,
        help=
        """Compare current prediction with its true label for every N steps""")
    PARSER.add_argument(
        "--decay_step",
        type=int,
        default=500,
        help="Learning rate will decay after N step")
    PARSER.add_argument(
        "--decay_percent",
        type=float,
        default=0.1,
        help="Percent of decreased in learning rate decay")
    PARSER.add_argument(
        "--model_dir",
        type=str,
        default="./transformer-cp.pt",
        help="Location to save the model")
    ARGS = PARSER.parse_args()

    ENCODER_UNITS = [int(unit) for unit in ARGS.encoder_units.split(",")]
    DECODER_UNITS = [int(unit) for unit in ARGS.decoder_units.split(",")]

    run(model_dir=ARGS.model_dir,
        enc_max_vocab=ARGS.enc_max_vocab,
        dec_max_vocab=ARGS.dec_max_vocab,
        encoder_emb_size=ARGS.encoder_emb_size,
        decoder_emb_size=ARGS.decoder_emb_size,
        encoder_units=ENCODER_UNITS,
        decoder_units=DECODER_UNITS,
        batch_size=ARGS.batch_size,
        epochs=ARGS.epochs,
        decay_step=ARGS.decay_step,
        decay_percent=ARGS.decay_percent,
        log_interval=ARGS.log_interval,
        save_interval=ARGS.save_interval,
        compare_interval=ARGS.compare_interval)
