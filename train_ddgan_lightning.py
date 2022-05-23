import argparse
import os
import shutil
import time
from distutils.util import strtobool

import numpy as np
import pytorch_lightning as pl
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import torchvision
import torchvision.transforms as transforms
from pytorch_lightning.loggers import WandbLogger
from torch import nn
from torch.nn import functional as F
from torch.utils.data import DataLoader
from torchvision import transforms
from torchvision.datasets import MNIST

import wandb
from EMA import EMA
from score_sde.models.discriminator import Discriminator_small
from score_sde.models.ncsnpp_generator_adagn import NCSNpp


def var_func_vp(t, beta_min, beta_max):
    log_mean_coeff = -0.25 * t ** 2 * (beta_max - beta_min) - 0.5 * t * beta_min
    var = 1.0 - torch.exp(2.0 * log_mean_coeff)
    return var


def var_func_geometric(t, beta_min, beta_max):
    return beta_min * ((beta_max / beta_min) ** t)


def extract(input, t, shape):
    out = torch.gather(input, 0, t)
    reshape = [shape[0]] + [1] * (len(shape) - 1)
    out = out.reshape(*reshape)

    return out


def get_time_schedule(args, device):
    n_timestep = args.num_timesteps
    eps_small = 1e-3
    t = np.arange(0, n_timestep + 1, dtype=np.float64)
    t = t / n_timestep
    t = torch.from_numpy(t) * (1.0 - eps_small) + eps_small
    return t.to(device)


def get_sigma_schedule(args, device):
    n_timestep = args.num_timesteps
    beta_min = args.beta_min
    beta_max = args.beta_max
    eps_small = 1e-3

    t = np.arange(0, n_timestep + 1, dtype=np.float64)
    t = t / n_timestep
    t = torch.from_numpy(t) * (1.0 - eps_small) + eps_small

    if args.use_geometric:
        var = var_func_geometric(t, beta_min, beta_max)
    else:
        var = var_func_vp(t, beta_min, beta_max)
    alpha_bars = 1.0 - var
    betas = 1 - alpha_bars[1:] / alpha_bars[:-1]

    first = torch.tensor(1e-8)
    betas = torch.cat((first[None], betas)).to(device)
    betas = betas.type(torch.float32)
    sigmas = betas ** 0.5
    a_s = torch.sqrt(1 - betas)
    return sigmas, a_s, betas


class Diffusion_Coefficients:
    def __init__(self, args, device):

        self.sigmas, self.a_s, _ = get_sigma_schedule(args, device=device)
        self.a_s_cum = np.cumprod(self.a_s.cpu())
        self.sigmas_cum = np.sqrt(1 - self.a_s_cum ** 2)
        self.a_s_prev = self.a_s.clone()
        self.a_s_prev[-1] = 1

        self.a_s_cum = self.a_s_cum.to(device)
        self.sigmas_cum = self.sigmas_cum.to(device)
        self.a_s_prev = self.a_s_prev.to(device)


def q_sample(coeff, x_start, t, device, *, noise=None):
    """
    Diffuse the data (t == 0 means diffused for t step)
    """
    if noise is None:
        noise = torch.randn_like(x_start)

    x_t = (
        extract(coeff.a_s_cum.to(device), t, x_start.shape) * x_start
        + extract(coeff.sigmas_cum.to(device), t, x_start.shape) * noise
    )

    return x_t


def q_sample_pairs(coeff, x_start, t, device):
    """
    Generate a pair of disturbed images for training
    :param x_start: x_0
    :param t: time step t
    :return: x_t, x_{t+1}
    """
    noise = torch.randn_like(x_start)
    x_t = q_sample(coeff, x_start, t, device)
    x_t_plus_one = (
        extract(coeff.a_s.to(device), t + 1, x_start.shape) * x_t
        + extract(coeff.sigmas.to(device), t + 1, x_start.shape) * noise
    )

    return x_t, x_t_plus_one


#%% posterior sampling
class Posterior_Coefficients:
    def __init__(self, args, device):

        _, _, self.betas = get_sigma_schedule(args, device=device)

        # we don't need the zeros
        self.betas = self.betas.type(torch.float32)[1:]

        self.alphas = 1 - self.betas
        self.alphas_cumprod = torch.cumprod(self.alphas, 0)
        self.alphas_cumprod_prev = torch.cat(
            (
                torch.tensor([1.0], dtype=torch.float32, device=device),
                self.alphas_cumprod[:-1],
            ),
            0,
        )
        self.posterior_variance = (
            self.betas * (1 - self.alphas_cumprod_prev) / (1 - self.alphas_cumprod)
        )

        self.sqrt_alphas_cumprod = torch.sqrt(self.alphas_cumprod)
        self.sqrt_recip_alphas_cumprod = torch.rsqrt(self.alphas_cumprod)
        self.sqrt_recipm1_alphas_cumprod = torch.sqrt(1 / self.alphas_cumprod - 1)

        self.posterior_mean_coef1 = (
            self.betas
            * torch.sqrt(self.alphas_cumprod_prev)
            / (1 - self.alphas_cumprod)
        )
        self.posterior_mean_coef2 = (
            (1 - self.alphas_cumprod_prev)
            * torch.sqrt(self.alphas)
            / (1 - self.alphas_cumprod)
        )

        self.posterior_log_variance_clipped = torch.log(
            self.posterior_variance.clamp(min=1e-20)
        )


def sample_posterior(coefficients, x_0, x_t, t, device):
    def q_posterior(x_0, x_t, t):
        mean = (
            extract(coefficients.posterior_mean_coef1.to(device), t, x_t.shape) * x_0
            + extract(coefficients.posterior_mean_coef2.to(device), t, x_t.shape) * x_t
        )
        var = extract(coefficients.posterior_variance.to(device), t, x_t.shape)
        log_var_clipped = extract(
            coefficients.posterior_log_variance_clipped.to(device), t, x_t.shape
        )
        return mean, var, log_var_clipped

    def p_sample(x_0, x_t, t):
        mean, _, log_var = q_posterior(x_0, x_t, t)

        noise = torch.randn_like(x_t)

        nonzero_mask = 1 - (t == 0).type(torch.float32)

        return (
            mean + nonzero_mask[:, None, None, None] * torch.exp(0.5 * log_var) * noise
        )

    sample_x_pos = p_sample(x_0, x_t, t)

    return sample_x_pos


def sample_from_model(coefficients, generator, n_time, x_init, T, opt):
    x = x_init
    with torch.no_grad():
        for i in reversed(range(n_time)):
            t = torch.full((x.size(0),), i, dtype=torch.int64).to(x.device)

            t_time = t
            latent_z = torch.randn(x.size(0), opt.nz, device=x.device)
            x_0 = generator(x, t_time, latent_z)
            x_new = sample_posterior(coefficients, x_0, x, t, device=x.device)
            x = x_new.detach()

    return x


class DDGAN(pl.LightningModule):
    def __init__(self, args):
        super().__init__()
        self.args = args
        self.automatic_optimization = False

        self.netG = NCSNpp(args)
        self.netD = Discriminator_small(
            nc=2 * args.num_channels,
            ngf=args.ngf,
            t_emb_dim=args.t_emb_dim,
            act=nn.LeakyReLU(0.2),
        )

        self.coeff = Diffusion_Coefficients(args, self.device)
        self.pos_coeff = Posterior_Coefficients(args, self.device)
        self.T = get_time_schedule(args, self.device)

        exp = args.exp
        parent_dir = "./saved_info/dd_gan/{}".format(args.dataset)

        self.exp_path = os.path.join(parent_dir, exp)
        if not os.path.exists(self.exp_path):
            os.makedirs(self.exp_path)

    def configure_optimizers(self):
        optimizerD = optim.Adam(
            self.netD.parameters(),
            lr=self.args.lr_d,
            betas=(self.args.beta1, args.beta2),
        )

        optimizerG = optim.Adam(
            self.netG.parameters(),
            lr=self.args.lr_g,
            betas=(self.args.beta1, args.beta2),
        )

        if self.args.use_ema:
            optimizerG = EMA(optimizerG, ema_decay=self.args.ema_decay)

        schedulerG = torch.optim.lr_scheduler.CosineAnnealingLR(
            optimizerG, self.args.num_epoch, eta_min=1e-5
        )
        schedulerD = torch.optim.lr_scheduler.CosineAnnealingLR(
            optimizerD, self.args.num_epoch, eta_min=1e-5
        )

        return (
            {"optimizer": optimizerG, "lr_scheduler": schedulerG},
            {"optimizer": optimizerD, "lr_scheduler": schedulerD},
        )

    def training_step(self, train_batch, batch_idx):
        batch_size = self.args.batch_size
        nz = self.args.nz

        epoch = self.trainer.current_epoch
        optimizerG, optimizerD = self.optimizers()
        schedulerG, schedulerD = self.lr_schedulers()

        for p in self.netD.parameters():
            p.requires_grad = True

        self.netD.zero_grad()

        # sample from p(x_0)
        real_data, _ = train_batch

        # sample t
        t = torch.randint(
            0, self.args.num_timesteps, (real_data.size(0),), device=self.device
        )

        x_t, x_tp1 = q_sample_pairs(self.coeff, real_data, t, device=self.device)

        # x_t, x_tp1 = x_t.type(t.type()), x_tp1.type(t.type())
        x_t.requires_grad = True

        # train with real
        D_real = self.netD(x_t, t, x_tp1.detach()).view(-1)

        errD_real = F.softplus(-D_real)
        errD_real = errD_real.mean()
        self.manual_backward(errD_real, retain_graph=True)

        if self.args.lazy_reg is None:
            grad_real = torch.autograd.grad(
                outputs=D_real.sum(), inputs=x_t, create_graph=True
            )[0]
            grad_penalty = (
                grad_real.view(grad_real.size(0), -1).norm(2, dim=1) ** 2
            ).mean()

            grad_penalty = args.r1_gamma / 2 * grad_penalty
            # grad_penalty.backward()
            self.manual_backward(grad_penalty)
        else:
            if self.global_step % args.lazy_reg == 0:
                grad_real = torch.autograd.grad(
                    outputs=D_real.sum(), inputs=x_t, create_graph=True
                )[0]
                grad_penalty = (
                    grad_real.view(grad_real.size(0), -1).norm(2, dim=1) ** 2
                ).mean()

                grad_penalty = args.r1_gamma / 2 * grad_penalty
                # grad_penalty.backward()
                self.manual_backward(grad_penalty)

        # train with fake
        latent_z = torch.randn(batch_size, nz, device=self.device)

        x_0_predict = self.netG(x_tp1.detach(), t, latent_z)
        x_pos_sample = sample_posterior(
            self.pos_coeff, x_0_predict, x_tp1, t, device=self.device
        )

        output = self.netD(x_pos_sample, t, x_tp1.detach()).view(-1)

        errD_fake = F.softplus(output)
        errD_fake = errD_fake.mean()
        self.manual_backward(errD_fake)

        errD = errD_real + errD_fake
        # Update D
        optimizerD.step()

        # update G
        for p in self.netD.parameters():
            p.requires_grad = False
        self.netG.zero_grad()

        t = torch.randint(
            0, args.num_timesteps, (real_data.size(0),), device=self.device
        )
        x_t, x_tp1 = q_sample_pairs(self.coeff, real_data, t, self.device)

        latent_z = torch.randn(batch_size, nz, device=self.device)

        x_0_predict = self.netG(x_tp1.detach(), t, latent_z)
        x_pos_sample = sample_posterior(
            self.pos_coeff, x_0_predict, x_tp1, t, device=self.device
        )

        output = self.netD(x_pos_sample, t, x_tp1.detach()).view(-1)

        errG = F.softplus(-output)
        errG = errG.mean()

        self.manual_backward(errG)
        optimizerG.step()

        wandb.log(
            {
                "train/lossG": errG.item(),
                "train/lossD": errD.item(),
                "train/global_step": self.global_step - 1,
            }
        )

        if self.trainer.is_last_batch:
            schedulerG.step()
            schedulerD.step()

        if epoch % 10 == 0:
            torchvision.utils.save_image(
                x_pos_sample,
                os.path.join(self.exp_path, "xpos_epoch_{}.png".format(epoch)),
                normalize=True,
            )
            if args.save_content:
                wandb.log({"x_posterior_sample": wandb.Image(x_pos_sample)})

        x_t_1 = torch.randn_like(real_data)
        fake_sample = sample_from_model(
            self.pos_coeff, self.netG, args.num_timesteps, x_t_1, self.T, args,
        )
        torchvision.utils.save_image(
            fake_sample,
            os.path.join(self.exp_path, "sample_discrete_epoch_{}.png".format(epoch)),
            normalize=True,
        )

        if args.save_content:
            wandb.log({"model_sample_discrete": wandb.Image(fake_sample)})

        if args.save_content:
            if epoch % args.save_content_every == 0:
                print("Saving content.")
                content = {
                    "epoch": epoch + 1,
                    "global_step": self.global_step,
                    "args": args,
                    "netG_dict": self.netG.state_dict(),
                    "optimizerG": optimizerG.state_dict(),
                    "schedulerG": schedulerG.state_dict(),
                    "netD_dict": self.netD.state_dict(),
                    "optimizerD": optimizerD.state_dict(),
                    "schedulerD": schedulerD.state_dict(),
                }

                file_path = os.path.join(self.exp_path, "content.pth")
                torch.save(content, file_path)

                wandb.save(file_path)
                wandb.log_artifact(file_path, name="content", type="training_content")

        if self.trainer.is_last_batch and epoch % self.args.save_ckpt_every == 0:
            if self.args.use_ema:
                optimizerG.swap_parameters_with_ema(store_params_in_ema=True)

            file_path = os.path.join(self.exp_path, "netG_{}.pth".format(epoch))
            torch.save(
                self.netG.state_dict(), file_path,
            )

            wandb.save(file_path)

            if self.args.use_ema:
                optimizerG.swap_parameters_with_ema(store_params_in_ema=True)

        return {"loss": errG}


if __name__ == "__main__":
    parser = argparse.ArgumentParser("ddgan parameters")
    parser.add_argument(
        "--seed", type=int, default=1024, help="seed used for initialization"
    )
    parser.add_argument(
        "--torch_deterministic",
        type=lambda x: bool(strtobool(x)),
        default=True,
        nargs="?",
        const=True,
        help="if toggled, `torch.backends.cudnn.deterministic=False`",
    )
    parser.add_argument(
        "--tpu", action="store_false", default=False, help="to use tpu or cuda"
    )

    parser.add_argument("--resume", action="store_true", default=False)

    parser.add_argument("--image_size", type=int, default=32, help="size of image")
    parser.add_argument("--num_channels", type=int, default=3, help="channel of image")
    parser.add_argument(
        "--centered", action="store_false", default=True, help="-1,1 scale"
    )
    parser.add_argument("--use_geometric", action="store_true", default=False)
    parser.add_argument(
        "--beta_min", type=float, default=0.1, help="beta_min for diffusion"
    )
    parser.add_argument(
        "--beta_max", type=float, default=20.0, help="beta_max for diffusion"
    )

    parser.add_argument(
        "--num_channels_dae",
        type=int,
        default=128,
        help="number of initial channels in denosing model",
    )
    parser.add_argument(
        "--n_mlp", type=int, default=3, help="number of mlp layers for z"
    )
    parser.add_argument("--ch_mult", nargs="+", type=int, help="channel multiplier")
    parser.add_argument(
        "--num_res_blocks",
        type=int,
        default=2,
        help="number of resnet blocks per scale",
    )
    parser.add_argument(
        "--attn_resolutions", default=(16,), help="resolution of applying attention"
    )
    parser.add_argument("--dropout", type=float, default=0.0, help="drop-out rate")
    parser.add_argument(
        "--resamp_with_conv",
        action="store_false",
        default=True,
        help="always up/down sampling with conv",
    )
    parser.add_argument(
        "--conditional", action="store_false", default=True, help="noise conditional"
    )
    parser.add_argument("--fir", action="store_false", default=True, help="FIR")
    parser.add_argument("--fir_kernel", default=[1, 3, 3, 1], help="FIR kernel")
    parser.add_argument(
        "--skip_rescale", action="store_false", default=True, help="skip rescale"
    )
    parser.add_argument(
        "--resblock_type",
        default="biggan",
        help="tyle of resnet block, choice in biggan and ddpm",
    )
    parser.add_argument(
        "--progressive",
        type=str,
        default="none",
        choices=["none", "output_skip", "residual"],
        help="progressive type for output",
    )
    parser.add_argument(
        "--progressive_input",
        type=str,
        default="residual",
        choices=["none", "input_skip", "residual"],
        help="progressive type for input",
    )
    parser.add_argument(
        "--progressive_combine",
        type=str,
        default="sum",
        choices=["sum", "cat"],
        help="progressive combine method.",
    )

    parser.add_argument(
        "--embedding_type",
        type=str,
        default="positional",
        choices=["positional", "fourier"],
        help="type of time embedding",
    )
    parser.add_argument(
        "--fourier_scale", type=float, default=16.0, help="scale of fourier transform"
    )
    parser.add_argument("--not_use_tanh", action="store_true", default=False)

    # geenrator and training
    parser.add_argument(
        "--exp", default="experiment_cifar_default", help="name of experiment"
    )
    parser.add_argument(
        "--dataset", default="cifar10", type=str, help="name of dataset"
    )
    parser.add_argument("--nz", type=int, default=100)
    parser.add_argument("--num_timesteps", type=int, default=4)

    parser.add_argument("--z_emb_dim", type=int, default=256)
    parser.add_argument("--t_emb_dim", type=int, default=256)
    parser.add_argument("--batch_size", type=int, default=128, help="input batch size")
    parser.add_argument("--num_epoch", type=int, default=1200)
    parser.add_argument("--ngf", type=int, default=64)

    parser.add_argument("--lr_g", type=float, default=1.5e-4, help="learning rate g")
    parser.add_argument("--lr_d", type=float, default=1e-4, help="learning rate d")
    parser.add_argument("--beta1", type=float, default=0.5, help="beta1 for adam")
    parser.add_argument("--beta2", type=float, default=0.9, help="beta2 for adam")
    parser.add_argument("--no_lr_decay", action="store_true", default=False)

    parser.add_argument(
        "--use_ema", action="store_true", default=False, help="use EMA or not"
    )
    parser.add_argument(
        "--ema_decay", type=float, default=0.9999, help="decay rate for EMA"
    )

    parser.add_argument("--r1_gamma", type=float, default=0.05, help="coef for r1 reg")
    parser.add_argument(
        "--lazy_reg", type=int, default=None, help="lazy regulariation."
    )

    parser.add_argument("--save_content", action="store_true", default=False)
    parser.add_argument(
        "--save_content_every",
        type=int,
        default=50,
        help="save content for resuming every x epochs",
    )
    parser.add_argument(
        "--save_ckpt_every", type=int, default=25, help="save ckpt every x epochs"
    )

    args = parser.parse_args()

    # Seed everything
    pl.seed_everything(args.seed)

    if args.torch_deterministic:
        torch.backends.cudnn.determinstic = True
        torch.backends.cudnn.benchmark = False

    exp_name = f"{args.exp}_{int(time.time())}"

    dataset = MNIST(
        "./data",
        train=True,
        transform=transforms.Compose(
            [
                transforms.Resize(32),
                transforms.ToTensor(),
                transforms.Normalize((0.1307,), (0.3081,)),
            ]
        ),
        download=True,
    )

    train_loader = DataLoader(
        dataset,
        batch_size=args.batch_size,
        shuffle=False,
        num_workers=4,
        pin_memory=True,
        drop_last=True,
    )

    # model
    model = DDGAN(args)

    wandb_logger = WandbLogger(
        # project=f"ddgan-{args.dataset}",
        name=exp_name,
        tags=["ddgan", args.dataset],
        config=vars(args),
        save_code=True,
        log_model="all",
    )

    # log gradients, parameter histogram and model topology
    # wandb_logger.watch(model, log="all")

    # training
    trainer = pl.Trainer(
        gpus=1 if not args.tpu else None,
        tpu_cores=8 if args.tpu else None,
        num_nodes=1,
        precision=16,
        logger=wandb_logger,
        max_epochs=args.num_epoch,
        limit_val_batches=0,
        num_sanity_val_steps=0,
        # track_grad_norm=2,
        # detect_anomaly=True,
    )
    print("Starting training")
    trainer.fit(model, train_loader)
