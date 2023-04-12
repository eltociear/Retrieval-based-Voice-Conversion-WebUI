import sys,os
now_dir=os.getcwd()
sys.path.append(os.path.join(now_dir,"train"))
import utils
hps = utils.get_hparams()
os.environ["CUDA_VISIBLE_DEVICES"]=hps.gpus.replace("-",",")
n_gpus=len(hps.gpus.split("-"))
from random import shuffle
import traceback,json,argparse,itertools,math,torch,pdb
torch.backends.cudnn.deterministic = False
torch.backends.cudnn.benchmark = False
from torch import nn, optim
from torch.nn import functional as F
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter
import torch.multiprocessing as mp
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.cuda.amp import autocast, GradScaler
from infer_pack import commons

from time import time as ttime
from data_utils import TextAudioLoaderMultiNSFsid,TextAudioLoader, TextAudioCollateMultiNSFsid,TextAudioCollate, DistributedBucketSampler
from infer_pack.models import (
    SynthesizerTrnMs256NSFsid,SynthesizerTrnMs256NSFsid_nono,
    MultiPeriodDiscriminator,
)
from losses import generator_loss, discriminator_loss, feature_loss, kl_loss
from mel_processing import mel_spectrogram_torch, spec_to_mel_torch


global_step = 0



def main():
    # n_gpus = torch.cuda.device_count()
    os.environ["MASTER_ADDR"] = "localhost"
    os.environ["MASTER_PORT"] = "5555"


    mp.spawn(
        run,
        nprocs=n_gpus,
        args=(
            n_gpus,
            hps,
        ),
    )


def run(rank, n_gpus, hps):
    global global_step
    if rank == 0:
        logger = utils.get_logger(hps.model_dir)
        logger.info(hps)
        utils.check_git_hash(hps.model_dir)
        writer = SummaryWriter(log_dir=hps.model_dir)
        writer_eval = SummaryWriter(log_dir=os.path.join(hps.model_dir, "eval"))

    dist.init_process_group(
        backend="gloo", init_method="env://", world_size=n_gpus, rank=rank
    )
    torch.manual_seed(hps.train.seed)
    if torch.cuda.is_available(): torch.cuda.set_device(rank)

    if (hps.if_f0 == 1):train_dataset = TextAudioLoaderMultiNSFsid(hps.data.training_files, hps.data)
    else:train_dataset = TextAudioLoader(hps.data.training_files, hps.data)
    train_sampler = DistributedBucketSampler(
        train_dataset,
        hps.train.batch_size*n_gpus,
        # [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000, 1200,1400],  # 16s
        [100, 200, 300, 400, 500, 600, 700, 800, 900],  # 16s
        num_replicas=n_gpus,
        rank=rank,
        shuffle=True,
    )
    # It is possible that dataloader's workers are out of shared memory. Please try to raise your shared memory limit.
    # num_workers=8 -> num_workers=4
    if (hps.if_f0 == 1):collate_fn = TextAudioCollateMultiNSFsid()
    else:collate_fn = TextAudioCollate()
    train_loader = DataLoader(
        train_dataset,
        num_workers=4,
        shuffle=False,
        pin_memory=True,
        collate_fn=collate_fn,
        batch_sampler=train_sampler,
        persistent_workers=True,
        prefetch_factor=8,
    )
    if(hps.if_f0==1):
        net_g = SynthesizerTrnMs256NSFsid(hps.data.filter_length // 2 + 1,hps.train.segment_size // hps.data.hop_length,**hps.model,is_half=hps.train.fp16_run,sr=hps.sample_rate)
    else:
        net_g = SynthesizerTrnMs256NSFsid_nono(hps.data.filter_length // 2 + 1,hps.train.segment_size // hps.data.hop_length,**hps.model,is_half=hps.train.fp16_run)
    if torch.cuda.is_available(): net_g = net_g.cuda(rank)
    net_d = MultiPeriodDiscriminator(hps.model.use_spectral_norm)
    if torch.cuda.is_available(): net_d = net_d.cuda(rank)
    optim_g = torch.optim.AdamW(
        net_g.parameters(),
        hps.train.learning_rate,
        betas=hps.train.betas,
        eps=hps.train.eps,
    )
    optim_d = torch.optim.AdamW(
        net_d.parameters(),
        hps.train.learning_rate,
        betas=hps.train.betas,
        eps=hps.train.eps,
    )
    # net_g = DDP(net_g, device_ids=[rank], find_unused_parameters=True)
    # net_d = DDP(net_d, device_ids=[rank], find_unused_parameters=True)
    if torch.cuda.is_available(): 
        net_g = DDP(net_g, device_ids=[rank])
        net_d = DDP(net_d, device_ids=[rank])
    else:
        net_g = DDP(net_g)
        net_d = DDP(net_d)

    try:#如果能加载自动resume
        _, _, _, epoch_str = utils.load_checkpoint(utils.latest_checkpoint_path(hps.model_dir, "D_*.pth"), net_d, optim_d)  # D多半加载没事
        if rank == 0:
            logger.info("loaded D")
        # _, _, _, epoch_str = utils.load_checkpoint(utils.latest_checkpoint_path(hps.model_dir, "G_*.pth"), net_g, optim_g,load_opt=0)
        _, _, _, epoch_str = utils.load_checkpoint(utils.latest_checkpoint_path(hps.model_dir, "G_*.pth"), net_g, optim_g)
        global_step = (epoch_str - 1) * len(train_loader)
        # epoch_str = 1
        # global_step = 0
    except:#如果首次不能加载，加载pretrain
        traceback.print_exc()
        epoch_str = 1
        global_step = 0
        if rank == 0:
            logger.info("loaded pretrained %s %s"%(hps.pretrainG,hps.pretrainD))
        print(net_g.module.load_state_dict(torch.load(hps.pretrainG,map_location="cpu")["model"]))##测试不加载优化器
        print(net_d.module.load_state_dict(torch.load(hps.pretrainD,map_location="cpu")["model"]))

    scheduler_g = torch.optim.lr_scheduler.ExponentialLR(
        optim_g, gamma=hps.train.lr_decay, last_epoch=epoch_str - 2
    )
    scheduler_d = torch.optim.lr_scheduler.ExponentialLR(
        optim_d, gamma=hps.train.lr_decay, last_epoch=epoch_str - 2
    )

    scaler = GradScaler(enabled=hps.train.fp16_run)

    cache=[]
    for epoch in range(epoch_str, hps.train.epochs + 1):
        if rank == 0:
            train_and_evaluate(
                rank,
                epoch,
                hps,
                [net_g, net_d],
                [optim_g, optim_d],
                [scheduler_g, scheduler_d],
                scaler,
                [train_loader, None],
                logger,
                [writer, writer_eval],cache
            )
        else:
            train_and_evaluate(
                rank,
                epoch,
                hps,
                [net_g, net_d],
                [optim_g, optim_d],
                [scheduler_g, scheduler_d],
                scaler,
                [train_loader, None],
                None,
                None,cache
            )
        scheduler_g.step()
        scheduler_d.step()


def train_and_evaluate(
    rank, epoch, hps, nets, optims, schedulers, scaler, loaders, logger, writers,cache
):
    net_g, net_d = nets
    optim_g, optim_d = optims
    train_loader, eval_loader = loaders
    if writers is not None:
        writer, writer_eval = writers

    train_loader.batch_sampler.set_epoch(epoch)
    global global_step

    net_g.train()
    net_d.train()
    if(cache==[]or hps.if_cache_data_in_gpu==False):#第一个epoch把cache全部填满训练集
        # print("caching")
        for batch_idx, info in enumerate(train_loader):
            if (hps.if_f0 == 1):phone,phone_lengths,pitch,pitchf,spec,spec_lengths,wave,wave_lengths,sid=info
            else:phone,phone_lengths,spec,spec_lengths,wave,wave_lengths,sid=info
            if torch.cuda.is_available():
                phone, phone_lengths = phone.cuda(rank, non_blocking=True), phone_lengths.cuda(rank, non_blocking=True )
                if (hps.if_f0 == 1):pitch,pitchf = pitch.cuda(rank, non_blocking=True),pitchf.cuda(rank, non_blocking=True)
                sid = sid.cuda(rank, non_blocking=True)
                spec, spec_lengths = spec.cuda(rank, non_blocking=True), spec_lengths.cuda(rank, non_blocking=True)
                wave, wave_lengths = wave.cuda(rank, non_blocking=True), wave_lengths.cuda(rank, non_blocking=True)
            if(hps.if_cache_data_in_gpu==True):
                if (hps.if_f0 == 1):cache.append((batch_idx, (phone,phone_lengths,pitch,pitchf,spec,spec_lengths,wave,wave_lengths ,sid)))
                else:cache.append((batch_idx, (phone,phone_lengths,spec,spec_lengths,wave,wave_lengths ,sid)))
            with autocast(enabled=hps.train.fp16_run):
                if (hps.if_f0 == 1):y_hat,ids_slice,x_mask,z_mask,(z, z_p, m_p, logs_p, m_q, logs_q) = net_g(phone, phone_lengths, pitch,pitchf, spec, spec_lengths,sid)
                else:y_hat,ids_slice,x_mask,z_mask,(z, z_p, m_p, logs_p, m_q, logs_q) = net_g(phone, phone_lengths, spec, spec_lengths,sid)
                mel = spec_to_mel_torch(spec,hps.data.filter_length,hps.data.n_mel_channels,hps.data.sampling_rate,hps.data.mel_fmin,hps.data.mel_fmax,)
                y_mel = commons.slice_segments(mel, ids_slice, hps.train.segment_size // hps.data.hop_length)
                with autocast(enabled=False):
                    y_hat_mel = mel_spectrogram_torch(
                        y_hat.float().squeeze(1),
                        hps.data.filter_length,
                        hps.data.n_mel_channels,
                        hps.data.sampling_rate,
                        hps.data.hop_length,
                        hps.data.win_length,
                        hps.data.mel_fmin,
                        hps.data.mel_fmax,
                    )
                if(hps.train.fp16_run==True):
                    y_hat_mel=y_hat_mel.half()
                wave = commons.slice_segments(
                    wave, ids_slice * hps.data.hop_length, hps.train.segment_size
                )  # slice

                # Discriminator
                y_d_hat_r, y_d_hat_g, _, _ = net_d(wave, y_hat.detach())
                with autocast(enabled=False):
                    loss_disc, losses_disc_r, losses_disc_g = discriminator_loss(
                        y_d_hat_r, y_d_hat_g
                    )
            optim_d.zero_grad()
            scaler.scale(loss_disc).backward()
            scaler.unscale_(optim_d)
            grad_norm_d = commons.clip_grad_value_(net_d.parameters(), None)
            scaler.step(optim_d)

            with autocast(enabled=hps.train.fp16_run):
                # Generator
                y_d_hat_r, y_d_hat_g, fmap_r, fmap_g = net_d(wave, y_hat)
                with autocast(enabled=False):
                    loss_mel = F.l1_loss(y_mel, y_hat_mel) * hps.train.c_mel
                    loss_kl = kl_loss(z_p, logs_q, m_p, logs_p, z_mask) * hps.train.c_kl
                    loss_fm = feature_loss(fmap_r, fmap_g)
                    loss_gen, losses_gen = generator_loss(y_d_hat_g)
                    loss_gen_all = loss_gen + loss_fm + loss_mel + loss_kl
            optim_g.zero_grad()
            scaler.scale(loss_gen_all).backward()
            scaler.unscale_(optim_g)
            grad_norm_g = commons.clip_grad_value_(net_g.parameters(), None)
            scaler.step(optim_g)
            scaler.update()

            if rank == 0:
                if global_step % hps.train.log_interval == 0:
                    lr = optim_g.param_groups[0]["lr"]
                    logger.info(
                        "Train Epoch: {} [{:.0f}%]".format(
                            epoch, 100.0 * batch_idx / len(train_loader)
                        )
                    )
                    # Amor For Tensorboard display
                    if loss_mel > 50:
                        loss_mel = 50
                    if loss_kl > 5:
                        loss_kl = 5

                    logger.info([global_step, lr])
                    logger.info(
                        f"loss_disc={loss_disc:.3f}, loss_gen={loss_gen:.3f}, loss_fm={loss_fm:.3f},loss_mel={loss_mel:.3f}, loss_kl={loss_kl:.3f}"
                    )
                    scalar_dict = {
                        "loss/g/total": loss_gen_all,
                        "loss/d/total": loss_disc,
                        "learning_rate": lr,
                        "grad_norm_d": grad_norm_d,
                        "grad_norm_g": grad_norm_g,
                    }
                    scalar_dict.update(
                        {"loss/g/fm": loss_fm, "loss/g/mel": loss_mel, "loss/g/kl": loss_kl}
                    )

                    scalar_dict.update(
                        {"loss/g/{}".format(i): v for i, v in enumerate(losses_gen)}
                    )
                    scalar_dict.update(
                        {"loss/d_r/{}".format(i): v for i, v in enumerate(losses_disc_r)}
                    )
                    scalar_dict.update(
                        {"loss/d_g/{}".format(i): v for i, v in enumerate(losses_disc_g)}
                    )
                    image_dict = {
                        "slice/mel_org": utils.plot_spectrogram_to_numpy(
                            y_mel[0].data.cpu().numpy()
                        ),
                        "slice/mel_gen": utils.plot_spectrogram_to_numpy(
                            y_hat_mel[0].data.cpu().numpy()
                        ),
                        "all/mel": utils.plot_spectrogram_to_numpy(
                            mel[0].data.cpu().numpy()
                        ),
                    }
                    utils.summarize(
                        writer=writer,
                        global_step=global_step,
                        images=image_dict,
                        scalars=scalar_dict,
                    )
            global_step += 1
        # if global_step % hps.train.eval_interval == 0:
        if epoch % hps.save_every_epoch == 0 and rank == 0:
            if(hps.if_latest==0):
                utils.save_checkpoint(
                    net_g,
                    optim_g,
                    hps.train.learning_rate,
                    epoch,
                    os.path.join(hps.model_dir, "G_{}.pth".format(global_step)),
                )
                utils.save_checkpoint(
                    net_d,
                    optim_d,
                    hps.train.learning_rate,
                    epoch,
                    os.path.join(hps.model_dir, "D_{}.pth".format(global_step)),
                )
            else:
                utils.save_checkpoint(
                    net_g,
                    optim_g,
                    hps.train.learning_rate,
                    epoch,
                    os.path.join(hps.model_dir, "G_{}.pth".format(2333333)),
                )
                utils.save_checkpoint(
                    net_d,
                    optim_d,
                    hps.train.learning_rate,
                    epoch,
                    os.path.join(hps.model_dir, "D_{}.pth".format(2333333)),
                )

    else:#后续的epoch直接使用打乱的cache
        shuffle(cache)
        # print("using cache")
        for batch_idx, info in cache:
            if (hps.if_f0 == 1):phone,phone_lengths,pitch,pitchf,spec,spec_lengths,wave,wave_lengths,sid=info
            else:phone,phone_lengths,spec,spec_lengths,wave,wave_lengths,sid=info
            with autocast(enabled=hps.train.fp16_run):
                if (hps.if_f0 == 1):y_hat,ids_slice,x_mask,z_mask,(z, z_p, m_p, logs_p, m_q, logs_q) = net_g(phone, phone_lengths, pitch,pitchf, spec, spec_lengths,sid)
                else:y_hat,ids_slice,x_mask,z_mask,(z, z_p, m_p, logs_p, m_q, logs_q) = net_g(phone, phone_lengths, spec, spec_lengths,sid)
                mel = spec_to_mel_torch(
                    spec,
                    hps.data.filter_length,
                    hps.data.n_mel_channels,
                    hps.data.sampling_rate,
                    hps.data.mel_fmin,
                    hps.data.mel_fmax,
                )
                y_mel = commons.slice_segments(
                    mel, ids_slice, hps.train.segment_size // hps.data.hop_length
                )
                with autocast(enabled=False):
                    y_hat_mel = mel_spectrogram_torch(
                        y_hat.float().squeeze(1),
                        hps.data.filter_length,
                        hps.data.n_mel_channels,
                        hps.data.sampling_rate,
                        hps.data.hop_length,
                        hps.data.win_length,
                        hps.data.mel_fmin,
                        hps.data.mel_fmax,
                    )
                if(hps.train.fp16_run==True):
                    y_hat_mel=y_hat_mel.half()
                wave = commons.slice_segments(
                    wave, ids_slice * hps.data.hop_length, hps.train.segment_size
                )  # slice

                # Discriminator
                y_d_hat_r, y_d_hat_g, _, _ = net_d(wave, y_hat.detach())
                with autocast(enabled=False):
                    loss_disc, losses_disc_r, losses_disc_g = discriminator_loss(
                        y_d_hat_r, y_d_hat_g
                    )
            optim_d.zero_grad()
            scaler.scale(loss_disc).backward()
            scaler.unscale_(optim_d)
            grad_norm_d = commons.clip_grad_value_(net_d.parameters(), None)
            scaler.step(optim_d)

            with autocast(enabled=hps.train.fp16_run):
                # Generator
                y_d_hat_r, y_d_hat_g, fmap_r, fmap_g = net_d(wave, y_hat)
                with autocast(enabled=False):
                    loss_mel = F.l1_loss(y_mel, y_hat_mel) * hps.train.c_mel
                    loss_kl = kl_loss(z_p, logs_q, m_p, logs_p, z_mask) * hps.train.c_kl

                    loss_fm = feature_loss(fmap_r, fmap_g)
                    loss_gen, losses_gen = generator_loss(y_d_hat_g)
                    loss_gen_all = loss_gen + loss_fm + loss_mel + loss_kl
            optim_g.zero_grad()
            scaler.scale(loss_gen_all).backward()
            scaler.unscale_(optim_g)
            grad_norm_g = commons.clip_grad_value_(net_g.parameters(), None)
            scaler.step(optim_g)
            scaler.update()

            if rank == 0:
                if global_step % hps.train.log_interval == 0:
                    lr = optim_g.param_groups[0]["lr"]
                    logger.info(
                        "Train Epoch: {} [{:.0f}%]".format(
                            epoch, 100.0 * batch_idx / len(train_loader)
                        )
                    )
                    # Amor For Tensorboard display
                    if loss_mel > 50:
                        loss_mel = 50
                    if loss_kl > 5:
                        loss_kl = 5

                    logger.info([global_step, lr])
                    logger.info(
                        f"loss_disc={loss_disc:.3f}, loss_gen={loss_gen:.3f}, loss_fm={loss_fm:.3f},loss_mel={loss_mel:.3f}, loss_kl={loss_kl:.3f}"
                    )
                    scalar_dict = {
                        "loss/g/total": loss_gen_all,
                        "loss/d/total": loss_disc,
                        "learning_rate": lr,
                        "grad_norm_d": grad_norm_d,
                        "grad_norm_g": grad_norm_g,
                    }
                    scalar_dict.update(
                        {"loss/g/fm": loss_fm, "loss/g/mel": loss_mel, "loss/g/kl": loss_kl}
                    )

                    scalar_dict.update(
                        {"loss/g/{}".format(i): v for i, v in enumerate(losses_gen)}
                    )
                    scalar_dict.update(
                        {"loss/d_r/{}".format(i): v for i, v in enumerate(losses_disc_r)}
                    )
                    scalar_dict.update(
                        {"loss/d_g/{}".format(i): v for i, v in enumerate(losses_disc_g)}
                    )
                    image_dict = {
                        "slice/mel_org": utils.plot_spectrogram_to_numpy(
                            y_mel[0].data.cpu().numpy()
                        ),
                        "slice/mel_gen": utils.plot_spectrogram_to_numpy(
                            y_hat_mel[0].data.cpu().numpy()
                        ),
                        "all/mel": utils.plot_spectrogram_to_numpy(
                            mel[0].data.cpu().numpy()
                        ),
                    }
                    utils.summarize(
                        writer=writer,
                        global_step=global_step,
                        images=image_dict,
                        scalars=scalar_dict,
                    )
            global_step += 1
        # if global_step % hps.train.eval_interval == 0:
        if epoch % hps.save_every_epoch == 0 and rank == 0:
            if(hps.if_latest==0):
                utils.save_checkpoint(
                    net_g,
                    optim_g,
                    hps.train.learning_rate,
                    epoch,
                    os.path.join(hps.model_dir, "G_{}.pth".format(global_step)),
                )
                utils.save_checkpoint(
                    net_d,
                    optim_d,
                    hps.train.learning_rate,
                    epoch,
                    os.path.join(hps.model_dir, "D_{}.pth".format(global_step)),
                )
            else:
                utils.save_checkpoint(
                    net_g,
                    optim_g,
                    hps.train.learning_rate,
                    epoch,
                    os.path.join(hps.model_dir, "G_{}.pth".format(2333333)),
                )
                utils.save_checkpoint(
                    net_d,
                    optim_d,
                    hps.train.learning_rate,
                    epoch,
                    os.path.join(hps.model_dir, "D_{}.pth".format(2333333)),
                )


    if rank == 0:
        logger.info("====> Epoch: {}".format(epoch))
    if(epoch>=hps.total_epoch and rank == 0):
        logger.info("Training is done. The program is closed.")
        from process_ckpt import savee#def savee(ckpt,sr,if_f0,name,epoch):
        if hasattr(net_g, 'module'):ckpt = net_g.module.state_dict()
        else:ckpt = net_g.state_dict()
        logger.info("saving final ckpt:%s"%(savee(ckpt,hps.sample_rate,hps.if_f0,hps.name,epoch)))
        os._exit(2333333)


if __name__ == "__main__":
    main()