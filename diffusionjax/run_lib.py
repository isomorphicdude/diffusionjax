"""
Training and evaluation for score-based generative models.
"""
import jax
from jax import jit
import jax.random as random
import jax.numpy as jnp

from diffusionjax.utils import batch_mul, get_loss, get_score, get_sampler
from diffusionjax.models import MLP, CNN
import diffusionjax.sde as sde_lib
from diffusionjax.solvers import EulerMaruyama, Annealed
from diffusionjax.utils import get_step_fn

from torch.utils.data import DataLoader
import numpy as np
import optax
from functools import partial
import flax
import flax.jax_utils as flax_utils
from flax.training import checkpoints
from absl import flags
from tqdm import tqdm, trange
import gc
import io
import os
import time
from typing import Any
import datetime
import logging
from collections import defaultdict
import wandb
import orbax

FLAGS = flags.FLAGS
logger = logging.getLogger(__name__)


# The dataclass that stores all training states
@flax.struct.dataclass
class State:
    step: int
    opt_state: Any
    params: Any
    rng: Any
    lr: float


def get_sde(config):
    # Setup SDE
    if config.training.sde.lower()=='vpsde':
        sde = sde_lib.VP(beta_min=config.model.beta_min, beta_max=config.model.beta_max)
    elif config.training.sde.lower()=='vesde':
        sde = sde_lib.VE(sigma_min=config.model.sigma_min, sigma_max=config.model.sigma_max)
    else:
        raise NotImplementedError(f"SDE {config.training.SDE} unknown.")
    return sde


def get_optimizer(config):
    """Returns an optax optimizer object based on `config`."""
    if config.optim.warmup:
        schedule = optax.warmup_cosine_decay_schedule(
            init_value=0.0,
            peak_value=1.0,
            warmup_steps=config.optim.warmup,
            decay_steps=config.optim.warmup + 1,
            end_value=1.0,
            )
    else:
        schedule = config.optim.lr
    if config.optim.optimizer=='Adam':
        if config.optim.weight_decay:
            optimizer = optax.adamw(
                learning_rate=schedule, b1=config.optim.beta1, eps=config.optim.eps)
        else:
            optimizer = optax.adam(
                learning_rate=schedule, b1=config.optim.beta1, eps=config.optim.eps)
    else:
        raise NotImplementedError(
            'Optimiser {} not supported yet!'.format(config.optim.optimizer)
        )
    if config.optim.grad_clip:
        optimizer = optax.chain(
            optax.clip(config.optim.grad_clip),
            optimizer
        )
    return optimizer


def get_model(config):
    if config.model.name.lower()=='mlp':
        model = MLP()
    elif config.model.name.lower()=='cnn':
        model = CNN()
    else:
        raise NotImplementedError(f"Model {config.model.name} unknown.")
    return model


def get_solver(config, sde, score):
    if config.solver.outer_solver.lower()=="eulermaruyama":
        outer_solver = EulerMaruyama(sde.reverse(score),
                                     num_steps=config.solver.num_outer_steps,
                                     dt=config.solver.dt, epsilon=config.solver.epsilon)
    else:
        raise NotImplementedError(f"Solver {config.solver.outer_solver} unknown.")
    if config.solver.inner_solver is None:
        inner_solver = None
    elif config.solver.inner_solver.lower()=="annealed":
        inner_solver = Annealed(sde.corrector(sde_lib.UDLangevin, score), num_steps=config.solver.num_inner_steps, snr=config.solver.snr)
    else:
        raise NotImplementedError(f"Solver {config.solver.inner_solver} unknown.")
    return outer_solver, inner_solver


def numpy_collate(batch):
    if isinstance(batch[0], np.ndarray):
        return np.stack(batch)
    elif isinstance(batch[0], (tuple, list)):
        transposed = zip(*batch)
        return [numpy_collate(samples) for samples in transposed]
    else:
        return np.array(batch)


def jit_collate(n_jitted_steps, batch_size, batch):
    return np.reshape(batch, (n_jitted_steps, batch_size, -1))


def pmap_and_jit_collate(num_devices, n_jitted_steps, per_device_batch_size, batch):
    return np.reshape(batch, (num_devices, n_jitted_steps, per_device_batch_size, -1))


def pmap_collate(num_devices, per_device_batch_size, batch):
    return np.reshape(batch, (num_devices, per_device_batch_size, -1))


class NumpyLoader(DataLoader):
    def __init__(self, config, dataset,
                 shuffle=False, sampler=None,
                 batch_sampler=None, num_workers=0,
                 pin_memory=False, drop_last=False,
                 timeout=0, worker_init_fn=None):
        prod_batch_dims = config.training.batch_size * config.training.n_jitted_steps
        if config.training.pmap and config.training.n_jitted_steps != 1:
            collate_fn = partial(
                pmap_and_jit_collate, jax.local_device_count(),
                config.training.n_jitted_steps,
                config.training.batch_size // jax.local_device_count())
        elif config.training.pmap and config.training.n_jitted_steps==1:
            collate_fn = partial(
                pmap_collate, jax.local_device_count(),
                config.training.batch_size // jax.local_device_count())
        elif config.training.n_jitted_steps != 1:
            collate_fn = partial(
                jit_collate, config.training.n_jitted_steps, config.training.batch_size)
        else:
            collate_fn = numpy_collate

        super(self.__class__, self).__init__(dataset,
                                             batch_size=prod_batch_dims,
                                             shuffle=shuffle,
                                             sampler=sampler,
                                             batch_sampler=batch_sampler,
                                             num_workers=num_workers,
                                             collate_fn=collate_fn,
                                             pin_memory=pin_memory,
                                             drop_last=drop_last,
                                             timeout=timeout,
                                             worker_init_fn=worker_init_fn)


def train(sampling_shape, config, workdir, dataset):
    """ Train a score based generative model using stochastic gradient descent

    Args:
        sampling_shape : sampling shape may differ depending on the modality of data
        config: An ml-collections configuration to use.
        workdir: Working directory for checkpoints and TF summaries. If this
            contains checkpoint training will be resumed from the latest checkpoint.
        dataset: a valid `torch.DataLoader` class.

    Returns:

    """
    train_dataloader = NumpyLoader(config, dataset)
    eval_dataloader = NumpyLoader(config, dataset)

    jax.default_device = jax.devices()[0]
    # Tip: use `export CUDA_VISIBLE_DEVICES` to restrict the devices visible to jax
    # ... devices (GPUs/TPUs) must be all the same model for pmap to work
    num_devices =  int(jax.local_device_count())
    if jax.process_index()==0: print("num_devices={}, pmap={}".format(
        num_devices, config.training.pmap))

    # Create directories for experimental logs
    sample_dir = os.path.join(workdir, "samples")
    if not os.path.exists(sample_dir):
        os.mkdir(sample_dir)

    scaler = dataset.get_data_scaler(config)
    inverse_scaler = dataset.get_data_inverse_scaler(config)

    # eval_function = dataset.calculate_metrics_batch
    # metric_names = dataset.metric_names()

    if jax.process_index()==0:
      run = wandb.init(
          project="diffusionjax",
          config=config,
      )

    rng = random.PRNGKey(config.seed)

    # Initialize model
    rng, model_rng = random.split(rng, 2)
    model = get_model(config)
    # Initialize parameters
    params = model.init(
        model_rng,
        jnp.zeros(
            sampling_shape
        ),
        jnp.ones((sampling_shape[0],)))

    # Initialize optimizer
    optimizer = get_optimizer(config)
    opt_state = optimizer.init(params)

    state = State(step=0,
        opt_state=opt_state,
        params=params,
        lr=config.optim.lr,
        rng=rng)

    # Create checkpoints directory
    checkpoint_dir = os.path.join(workdir, "checkpoints")
    if not os.path.exists(checkpoint_dir):
        os.mkdir(checkpoint_dir)

    # Intermediate checkpoints to resume training after pre-emption in cloud environments
    checkpoint_meta_dir = os.path.join(workdir, "checkpoints-meta")
    if not os.path.exists(checkpoint_meta_dir):
        os.mkdir(checkpoint_meta_dir)

    # Orbax checkpointer boilerplate
    manager_options = orbax.checkpoint.CheckpointManagerOptions(
        create=True, max_to_keep=np.inf)
    checkpoint_manager = orbax.checkpoint.CheckpointManager(
        checkpoint_dir,
        orbax.checkpoint.Checkpointer(orbax.checkpoint.PyTreeCheckpointHandler()), manager_options)

    meta_manager_options = orbax.checkpoint.CheckpointManagerOptions(
        create=True, max_to_keep=1)
    meta_checkpoint_manager = orbax.checkpoint.CheckpointManager(
        checkpoint_meta_dir,
        orbax.checkpoint.Checkpointer(orbax.checkpoint.PyTreeCheckpointHandler()), manager_options)

    # Resume training when intermediate checkpoints are detected
    restore_args = flax.training.orbax_utils.restore_args_from_target(state, mesh=None)
    save_step = meta_checkpoint_manager.latest_step()
    if save_step is not None:
        meta_checkpoint_manager.restore(
            save_step,
            items=state, restore_kwargs={'restore_args': restore_args})

    # `state.step` is JAX integer on the GPU/TPU devices
    initial_step = int(state.step)
    rng = state.rng

    # Build one-step training and evaluation functions
    sde = get_sde(config)
    # Trained score
    score = get_score(sde, model, params, score_scaling=config.training.score_scaling)

    # Setup solver
    outer_solver, inner_solver = get_solver(config, sde, score)

    loss = get_loss(
        sde, outer_solver, model,
        score_scaling=config.training.score_scaling,
        likelihood_weighting=config.training.likelihood_weighting)

    train_step = get_step_fn(loss, optimizer, train=True, pmap=config.training.pmap)
    eval_step = get_step_fn(loss, optimizer, train=False, pmap=config.training.pmap)

    if config.training.n_jitted_steps > 1:
        train_step = partial(jax.lax.scan, train_step)
        eval_step = partial(jax.lax.scan, eval_step)

    if config.training.pmap:
        train_step = jax.pmap(
            train_step, axis_name='batch', donate_argnums=1)
        eval_step = jax.pmap(
            eval_step, axis_name='batch', donate_argnums=1)

    # Replicate the training state to run on multiple devices
    if config.training.pmap: state = flax_utils.replicate(state)

    # Probably want to train over multiple epochs
    # If num_epochs > num_batch, decides which tqdm to go over
    i_epoch = 0
    prev_time = time.time()

    # Deal with training in a number of steps
    # num_epochs = config.training.n_iters // (dataset_size / batch_size)
    num_epochs = config.training.n_iters

    step = initial_step
    # In case there are multiple hosts (e.g., TPU pods), only log to host 0
    if jax.process_index() == 0:
        logging.info("Starting training loop at step %d." % (initial_step,))
    rng = jax.random.fold_in(rng, jax.process_index())

    # JIT multiple training steps together for faster training
    n_jitted_steps = config.training.n_jitted_steps

    # Must be divisible by the number of steps jitted together
    assert config.training.log_step_freq % n_jitted_steps == 0 and \
        config.training.snapshot_freq_for_preemption % n_jitted_steps == 0 and \
        config.training.eval_freq % n_jitted_steps == 0 and \
        config.training.snapshot_freq % n_jitted_steps == 0, "Missing logs or checkpoints!"

    mean_losses = jnp.zeros((num_epochs, 1))
    for i_epoch in trange(1, num_epochs + 1, unit="epochs"):
        current_time = time.time()

        if i_epoch != 0 and (num_epochs < config.training.batch_size):
            print("Epoch took {:.1f} seconds".format(current_time - prev_time))
            prev_time = time.time()

        eval_iter = iter(eval_dataloader)

        with tqdm(
                train_dataloader,
                unit=" batch",
                disable=True
        ) as tepoch:
            tepoch.set_description(f"Epoch {i_epoch}")
            losses = jnp.empty((len(tepoch), 1))

            for i_batch, batch in enumerate(tepoch):
                batch = jax.tree_map(lambda x: scaler(x), batch)

                # Execute one training step
                if config.training.pmap:
                    rng, *next_rng = jax.random.split(rng, num=jax.local_device_count() + 1)
                    next_rng = jnp.asarray(next_rng)
                else:
                    rng, next_rng = jax.random.split(rng, num=2)

                (_, params, opt_state), loss_train = train_step(
                    (next_rng, state.params, state.opt_state), batch)
                state = state.replace(opt_state=opt_state, params=params)

                if config.training.pmap:
                    loss_train = flax_utils.unreplicate(loss_train).mean()  # returns a single instance of replicated loss array
                else:
                    loss_train = loss_train.mean()

                # Log to console, file and wandb on host 0
                if jax.process_index()==0:
                    step += config.training.n_jitted_steps
                    losses = losses.at[i_batch].set(loss_train)
                    if step % config.training.log_step_freq==0 and jax.process_index() == 0:
                        logging.info("step {:d}, training_loss {:.2e}".format(step, loss_train))

                # Save a temporary checkpoint to resume training after pre-emption (for cloud computing environments) periodically
                if step!=0 and step % config.training.snapshot_freq_for_preemption==0 and jax.process_index()==0:
                    if config.training.pmap:
                        saved_state = flax_utils.unreplicate(state)
                    else:
                        saved_state = state
                    saved_state = saved_state.replace(rng=rng)
                    saved_args = flax.training.orbax_utils.save_args_from_target(saved_state)
                    meta_checkpoint_manager.save(step//config.training.snapshot_freq_for_preemption, saved_state, save_kwargs={'save_args': saved_args})

                # Report the loss on an evaluation dataset periodically
                if step % config.training.eval_freq == 0:
                    eval_batch = jax.tree_map(lambda x: scaler(x), next(eval_iter))
                    if config.training.pmap:
                        rng, *next_rng = jax.random.split(rng, num=jax.local_device_count() + 1)
                        next_rng = jnp.asarray(next_rng)
                    else:
                        rng, next_rng = jax.random.split(rng, num=2)
                    (_, _, _), loss_eval = eval_step(
                        (next_rng, state.params, state.opt_state), eval_batch)

                    if config.training.pmap:
                        loss_eval = flax_utils.unreplicate(loss_eval).mean()
                    else:
                        loss_eval = loss_eval.mean()

                    if jax.process_index()==0:
                        logging.info("batch: {:d}, eval_loss: {:.5e}".format(step, loss_eval))
                        wandb.log({"eval-loss": loss_eval})

                # Save a checkpoint periodically and generate samples if needed
                if step != 0 and step % config.training.snapshot_freq == 0 or step == config.training.n_iters:
                    # Save the checkpoint
                    if jax.process_index()==0:
                        if config.training.pmap:
                            saved_state = flax_utils.unreplicate(state)
                        else:
                            saved_state = state
                        saved_state = saved_state.replace(rng=rng)
                        saved_args = flax.training.orbax_utils.save_args_from_target(saved_state)
                        checkpoint_manager.save(step // config.training.snapshot_freq, saved_state, save_kwargs={'save_args': saved_args})

                    # Generate and save samples
                    if config.training.snapshot_sampling:
                        # Setup solver with new trained score
                        # Use the unreplicated parameters of the saved state
                        score = get_score(
                            sde, model, saved_state.params, config.training.score_scaling)
                        outer_solver, inner_solver = get_solver(config, sde, score)
                        sampler = get_sampler(sampling_shape, outer_solver,
                                            inner_solver, denoise=config.sampling.denoise,
                                            stack_samples=config.sampling.stack_samples,
                                            inverse_scaler=inverse_scaler)

                        if config.training.pmap:
                            sampler = jax.pmap(sampler, axis_name='batch')
                            rng, *sample_rng = random.split(rng, 1 + jax.local_device_count())
                            sample_rng = jnp.asarray(sample_rng)
                        else:
                            rng, sample_rng = random.split(rng, 2)

                        sample, nfe = sampler(sample_rng)

                        # eval_fn = eval_function(sample)
                        # wandb.log({metric_names[0]: eval_fn})

                        this_sample_dir = os.path.join(
                            sample_dir, "iter_{}_host_{}".format(step, jax.process_index()))
                        if not os.path.isdir(this_sample_dir):
                            os.mkdir(this_sample_dir)

                        with open(os.path.join(this_sample_dir, "sample.np"), 'wb') as infile:
                            np.save(infile, sample)

            if jax.process_index()==0:
                mean_loss = jnp.mean(losses, axis=0)

        if jax.process_index()==0 and i_epoch % config.training.log_epoch_freq==0:
            logging.info("step {:d}, mean_loss {:.2e}".format(step, mean_loss[0]))
            mean_losses = mean_losses.at[i_epoch].set(mean_loss)
            wandb.log({"train-loss": mean_loss})

    artifact = wandb.Artifact(name='checkpoint', type='checkpoint')
    artifact.add_dir(local_path=checkpoint_dir)
    run.log_artifact(artifact)

    # Get the model and do test dataset
    if config.training.pmap:
        saved_state = flax_utils.unreplicate(state)
    else:
        saved_state = state
    return saved_state.params, saved_state.opt_state, mean_losses