"""
This is the rendering logic that used to live in the notebook.
It's sort of a mess in here. I'm working on it.
Thank you for your patience.-- The Management
"""

import gc
import glob
import json
from pathlib import Path
import math
import os
from os.path import exists as path_exists
import re
import sys
import subprocess
import warnings

import hydra
from loguru import logger
from omegaconf import OmegaConf, DictConfig

import numpy as np
import pandas as pd
from PIL import Image, ImageEnhance
import torch
from torch.utils.tensorboard import SummaryWriter
from torchvision.transforms import functional as TF

# Deprecate or move functionality somewhere less general
import matplotlib.pyplot as plt
import seaborn as sns
from IPython import display

logger.info("Loading pytti...")
from pytti.Notebook import (
    is_notebook,
    change_tqdm_color,  # why though?
    get_last_file,
    get_next_file,
    make_hbox,
    load_settings,  # hydra should handle this stuff
    write_settings,
    save_settings,
    save_batch,
    CLIP_MODEL_NAMES,
    load_clip,
    get_frames,
    build_loss,
    #    format_params,
    #    rotoscopers,
    #    clear_rotoscopers,
    #    update_rotoscopers,
    #    Rotoscoper,
)

from pytti import Perceptor
from pytti.Image import PixelImage, RGBImage, VQGANImage
from pytti.ImageGuide import DirectImageGuide
from pytti.Perceptor.Embedder import HDMultiClipEmbedder
from pytti.Perceptor.Prompt import parse_prompt
from pytti.LossAug import TVLoss, HSVLoss, OpticalFlowLoss, TargetFlowLoss
from pytti.Transforms import zoom_2d, zoom_3d, apply_flow
from pytti import (
    DEVICE,
    fetch,
    parametric_eval,
    set_t,
    vram_usage_mode,
    print_vram_usage,
    reset_vram_usage,
    freeze_vram_usage,
    vram_profiling,
)
from pytti.LossAug.DepthLoss import init_AdaBins

# from pytti.rotoscoper import ROTOSCOPERS
from pytti.rotoscoper import (
    clear_rotoscopers,
    update_rotoscopers,
)
from pytti.Transforms import (
    animate_2d,
    zoom_3d,
    animate_video_source,
)

logger.info("pytti loaded.")

change_tqdm_color()

logger.debug(sys.path)


sns.set()
plt.style.use("bmh")
pd.options.display.max_columns = None
pd.options.display.width = 175


TB_LOGDIR = "logs"  # to do: make this more easily configurable
writer = SummaryWriter(TB_LOGDIR)
OUTPATH = f"{os.getcwd()}/images_out/"


#######################################################


def configure_init_image(
    init_image_pil: Image.Image,
    restore: bool,
    img: PixelImage,
    params,
    loss_augs,
):

    if init_image_pil is not None:
        if not restore:
            # move these logging statements into .encode_image()
            logger.info("Encoding image...")
            img.encode_image(init_image_pil)
            logger.info("Encoded Image:")
            # pretty sure this assumes we're in a notebook
            display.display(img.decode_image())
        # set up init image prompt
        init_augs = ["direct_init_weight"]
        init_augs = [
            build_loss(
                x,
                params[x],
                f"init image ({params.init_image})",
                img,
                init_image_pil,
            )
            for x in init_augs
            if params[x] not in ["", "0"]
        ]
        loss_augs.extend(init_augs)
        if params.semantic_init_weight not in ["", "0"]:
            semantic_init_prompt = parse_prompt(
                embedder,
                f"init image [{params.init_image}]:{params.semantic_init_weight}",
                init_image_pil,
            )
            prompts[0].append(semantic_init_prompt)
        else:
            semantic_init_prompt = None
    else:
        init_augs, semantic_init_prompt = [], None

    return init_augs, semantic_init_prompt, loss_augs, img


def configure_stabilization_augs(img, init_image_pil, params, loss_augs):
    ## NB: in loss orchestrator impl, this begins with an init_image override.
    ## possibly the source of the issue?
    stabilization_augs = [
        "direct_stabilization_weight",
        "depth_stabilization_weight",
        "edge_stabilization_weight",
    ]
    stabilization_augs = [
        build_loss(x, params[x], "stabilization", img, init_image_pil)
        for x in stabilization_augs
        if params[x] not in ["", "0"]
    ]
    loss_augs.extend(stabilization_augs)

    return loss_augs, img, init_image_pil, stabilization_augs


def configure_optical_flows(img, params, loss_augs):

    if params.animation_mode == "Video Source":
        if params.flow_stabilization_weight == "":
            params.flow_stabilization_weight = "0"
        optical_flows = [
            OpticalFlowLoss.TargetImage(
                f"optical flow stabilization (frame {-2**i}):{params.flow_stabilization_weight}",
                img.image_shape,
            )
            for i in range(params.flow_long_term_samples + 1)
        ]
        for optical_flow in optical_flows:
            optical_flow.set_enabled(False)
        loss_augs.extend(optical_flows)
    elif params.animation_mode == "3D" and params.flow_stabilization_weight not in [
        "0",
        "",
    ]:
        optical_flows = [
            TargetFlowLoss.TargetImage(
                f"optical flow stabilization:{params.flow_stabilization_weight}",
                img.image_shape,
            )
        ]
        for optical_flow in optical_flows:
            optical_flow.set_enabled(False)
        loss_augs.extend(optical_flows)
    else:
        optical_flows = []
    # other loss augs
    if params.smoothing_weight != 0:
        loss_augs.append(TVLoss(weight=params.smoothing_weight))

    return img, loss_augs, optical_flows


#######################################################


def parse_scenes(
    embedder,
    scenes,
    scene_prefix,
    scene_suffix,
):
    """
    Parses scenes separated by || and applies provided prefixes and suffixes to each scene.
    :param embedder: The embedder object
    :param params: The experiment parameters
    :return: The embedder and the prompts.
    """
    logger.info("Loading prompts...")
    prompts = [
        [
            parse_prompt(embedder, p.strip())
            for p in (scene_prefix + stage + scene_suffix).strip().split("|")
            if p.strip()
        ]
        for stage in scenes.split("||")
        if stage
    ]
    logger.info("Prompts loaded.")
    return embedder, prompts


def load_init_image(
    init_image_path=None,
    height: int = -1,  # why '-1'? should be None. Or btter yet, assert that it's greater than zero
    width: int = -1,
):
    """
    If the user has specified an image to use as the initial image, load it. Otherwise, if the
    user has specified a width or height, create a blank image of the specified size
    :param init_image_path: A local path or URL describing where to load the image from
    :param height: height of the image to be generated.
    :return: the initial image and the size of the initial image.
    """
    if init_image_path:
        init_image_pil = Image.open(fetch(init_image_path)).convert("RGB")
        init_size = init_image_pil.size
        # automatic aspect ratio matching
        if width == -1:
            width = int(height * init_size[0] / init_size[1])
        if height == -1:
            height = int(width * init_size[1] / init_size[0])
    else:
        init_image_pil = None
    return init_image_pil, height, width


def load_video_source(
    video_path: str,
    pre_animation_steps: int,
    steps_per_frame: int,
    height: int,
    width: int,
    init_image_pil: Image.Image,
):
    """
    Loads a video file and returns a PIL image of the first frame
    :param video_path: The path to the video file
    :param pre_animation_steps: The number of frames to skip at the beginning of the video
    :param steps_per_frame: How many steps to take per frame
    :param height: the height of the output image
    :param width: the width of the output image
    :return: The video frames, the initial image, the height and width of the image.
    """
    logger.info(f"loading {video_path}...")
    video_frames = get_frames(video_path)
    pre_animation_steps = max(steps_per_frame, pre_animation_steps)
    if init_image_pil is None:
        init_image_pil = Image.fromarray(video_frames.get_data(0)).convert("RGB")
        # enhancer = ImageEnhance.Contrast(init_image_pil)
        # init_image_pil = enhancer.enhance(2)
        init_size = init_image_pil.size
        if width == -1:
            width = int(height * init_size[0] / init_size[1])
        if height == -1:
            height = int(width * init_size[1] / init_size[0])
    return video_frames, init_image_pil, height, width


#######################################################


@hydra.main(config_path="config", config_name="default")
def _main(cfg: DictConfig):
    # params = OmegaConf.to_container(cfg, resolve=True)
    params = cfg
    logger.debug(params)
    logger.debug(OmegaConf.to_container(cfg, resolve=True))
    latest = -1

    batch_mode = False  # @param{type:"boolean"}

    ### Move these into default.yaml
    # @markdown check `restore` to restore from a previous run
    restore = False  # @param{type:"boolean"}
    # @markdown check `reencode` if you are restoring with a modified image or modified image settings
    reencode = False  # @param{type:"boolean"}
    # @markdown which run to restore
    restore_run = latest  # @param{type:"raw"}

    # NB: `backup/` dir probably not working at present
    if restore and restore_run == latest:
        _, restore_run = get_last_file(
            f"backup/{params.file_namespace}",
            f"^(?P<pre>{re.escape(params.file_namespace)}\\(?)(?P<index>\\d*)(?P<post>\\)?_\\d+\\.bak)$",
        )

    # I feel like there's probably no reason this is defined inside of _main()
    def do_run():

        # Phase 1 - reset state
        ########################

        clear_rotoscopers()  # what a silly name
        vram_profiling(params.approximate_vram_usage)
        reset_vram_usage()
        global CLIP_MODEL_NAMES  # We're gonna do something about these globals
        # @markdown which frame to restore from
        restore_frame = latest  # @param{type:"raw"}

        # set up seed for deterministic RNG
        if params.seed is not None:
            torch.manual_seed(params.seed)

        # Phase 2 - load and parse
        ###########################

        # load CLIP
        load_clip(params)

        cutn = params.cutouts
        if params.gradient_accumulation_steps > 1:
            try:
                assert cutn % params.gradient_accumulation_steps == 0
            except:
                logger.warning(
                    "To use GRADIENT_ACCUMULATION_STEPS > 1, "
                    "the CUTOUTS parameter must be a scalar multiple of "
                    "GRADIENT_ACCUMULATION_STEPS. I.e `STEPS/CUTS` must have no remainder."
                )
                raise
            cutn //= params.gradient_accumulation_steps
        logger.debug(cutn)

        embedder = HDMultiClipEmbedder(
            # cutn=params.cutouts,
            cutn=cutn,
            cut_pow=params.cut_pow,
            padding=params.cutout_border,
            border_mode=params.border_mode,
        )

        # load scenes
        with vram_usage_mode("Text Prompts"):
            embedder, prompts = parse_scenes(
                embedder,
                scenes=params.scenes,
                scene_prefix=params.scene_prefix,
                scene_suffix=params.scene_suffix,
            )

        # load init image
        init_image_pil, height, width = load_init_image(
            init_image_path=params.init_image,
            height=params.height,
            width=params.width,
        )

        # video source
        video_frames = None
        if params.animation_mode == "Video Source":
            video_frames, init_image_pil, height, width = load_video_source(
                video_path=params.video_path,
                pre_animation_steps=params.pre_animation_steps,
                steps_per_frame=params.steps_per_frame,
                height=params.height,
                width=params.width,
                init_image_pil=init_image_pil,
            )

        params.height, params.width = height, width

        # Phase 3 - Setup Optimization
        ###############################

        # set up image
        if params.image_model == "Limited Palette":
            img = PixelImage(
                width=params.width,
                height=params.height,
                scale=params.pixel_size,  # NB: inconsistent naming
                pallet_size=params.palette_size,  # NB: inconsistent naming
                n_pallets=params.palettes,  # NB: inconsistent naming
                gamma=params.gamma,
                hdr_weight=params.hdr_weight,
                norm_weight=params.palette_normalization_weight,
            )
            img.encode_random(random_pallet=params.random_initial_palette)
            if params.target_palette.strip() != "":
                img.set_pallet_target(
                    Image.open(fetch(params.target_palette)).convert("RGB")
                )
            else:
                img.lock_pallet(params.lock_palette)
        elif params.image_model == "Unlimited Palette":
            img = RGBImage(params.width, params.height, params.pixel_size)
            img.encode_random()
        elif params.image_model == "VQGAN":
            model_artifacts_path = Path(params.models_parent_dir) / "vqgan"
            VQGANImage.init_vqgan(params.vqgan_model, model_artifacts_path)
            img = VQGANImage(params.width, params.height, params.pixel_size)
            img.encode_random()

        loss_augs = []

        #####################
        # set up init image #
        #####################

        (init_augs, semantic_init_prompt, loss_augs, img) = configure_init_image(
            init_image_pil,
            restore,
            img,
            params,
            loss_augs,
        )

        # other image prompts

        loss_augs.extend(
            type(img)
            .get_preferred_loss()
            .TargetImage(p.strip(), img.image_shape, is_path=True)
            for p in params.direct_image_prompts.split("|")
            if p.strip()
        )

        # stabilization
        (
            loss_augs,
            img,
            init_image_pil,
            stabilization_augs,
        ) = configure_stabilization_augs(img, init_image_pil, params, loss_augs)

        ############################
        ### I think this bit might've been lost in the shuffle?

        if params.semantic_stabilization_weight not in ["0", ""]:
            last_frame_semantic = parse_prompt(
                embedder,
                f"stabilization:{params.semantic_stabilization_weight}",
                init_image_pil if init_image_pil else img.decode_image(),
            )
            last_frame_semantic.set_enabled(init_image_pil is not None)
            for scene in prompts:
                scene.append(last_frame_semantic)
        else:
            last_frame_semantic = None

        ###
        ############################

        # optical flow
        img, loss_augs, optical_flows = configure_optical_flows(img, params, loss_augs)

        # Phase 4 - setup outputs
        ##########################

        # Transition as much of this as possible to hydra

        # set up filespace
        Path(f"{OUTPATH}/{params.file_namespace}").mkdir(parents=True, exist_ok=True)
        Path(f"backup/{params.file_namespace}").mkdir(parents=True, exist_ok=True)
        if restore:
            base_name = (
                params.file_namespace
                if restore_run == 0
                else f"{params.file_namespace}({restore_run})"
            )
        elif not params.allow_overwrite:
            # finds the next available base_name to save files with. Why did I do this with regex?
            _, i = get_next_file(
                f"{OUTPATH}/{params.file_namespace}",
                f"^(?P<pre>{re.escape(params.file_namespace)}\\(?)(?P<index>\\d*)(?P<post>\\)?_1\\.png)$",
                [f"{params.file_namespace}_1.png", f"{params.file_namespace}(1)_1.png"],
            )
            base_name = (
                params.file_namespace if i == 0 else f"{params.file_namespace}({i})"
            )
        else:
            base_name = params.file_namespace

        # restore
        if restore:
            if not reencode:
                if restore_frame == latest:
                    filename, restore_frame = get_last_file(
                        f"backup/{params.file_namespace}",
                        f"^(?P<pre>{re.escape(base_name)}_)(?P<index>\\d*)(?P<post>\\.bak)$",
                    )
                else:
                    filename = f"{base_name}_{restore_frame}.bak"
                logger.info("restoring from", filename)
                img.load_state_dict(
                    torch.load(f"backup/{params.file_namespace}/{filename}")
                )
            else:  # reencode
                if restore_frame == latest:
                    filename, restore_frame = get_last_file(
                        f"{OUTPATH}/{params.file_namespace}",
                        f"^(?P<pre>{re.escape(base_name)}_)(?P<index>\\d*)(?P<post>\\.png)$",
                    )
                else:
                    filename = f"{base_name}_{restore_frame}.png"
                logger.info("restoring from", filename)
                img.encode_image(
                    Image.open(f"{OUTPATH}/{params.file_namespace}/{filename}").convert(
                        "RGB"
                    )
                )
            i = restore_frame * params.save_every
        else:
            i = 0

        ## tensorboard should handle this stuff.

        # graphs
        if params.show_graphs:
            fig, axs = plt.subplots(4, 1, figsize=(21, 13))
            axs = np.asarray(axs).flatten()
            # fig.facecolor = (0,0,0)
        else:
            fig, axs = None, None

        # Phase 5 - setup optimizer
        ############################

        # make the main model object
        model = DirectImageGuide(
            img,
            embedder,
            lr=params.learning_rate,
            params=params,
            writer=writer,
            fig=fig,
            axs=axs,
            base_name=base_name,
            optical_flows=optical_flows,
            video_frames=video_frames,
            stabilization_augs=stabilization_augs,
            last_frame_semantic=last_frame_semantic,
            # embedder=embedder,
            init_augs=init_augs,
            semantic_init_prompt=semantic_init_prompt,
        )

        from pytti.update_func import update

        model.update = update

        # Pretty sure this isn't necessary, Hydra should take care of saving
        # the run settings now
        settings_path = f"{OUTPATH}/{params.file_namespace}/{base_name}_settings.txt"
        logger.info(f"Settings saved to {settings_path}")
        save_settings(params, settings_path)

        # Run the training loop
        ########################

        # what are these skip variables doing?
        skip_prompts = i // params.steps_per_scene
        skip_steps = i % params.steps_per_scene
        last_scene = prompts[0] if skip_prompts == 0 else prompts[skip_prompts - 1]
        for scene in prompts[skip_prompts:]:
            logger.info("Running prompt:", " | ".join(map(str, scene)))
            i += model.run_steps(
                params.steps_per_scene - skip_steps,
                scene,
                last_scene,
                loss_augs,
                interp_steps=params.interpolation_steps,
                i_offset=i,
                skipped_steps=skip_steps,
                gradient_accumulation_steps=params.gradient_accumulation_steps,
            )
            skip_steps = 0
            model.clear_dataframe()
            last_scene = scene

        # tensorboard summarywriter should supplant all our graph stuff
        if fig:
            del fig, axs
        ############################# DMARX
        writer.close()
        #############################

    ## Work on getting rid of this batch mode garbage. Hydra's got this.
    try:
        gc.collect()
        torch.cuda.empty_cache()
        if batch_mode:
            if restore:
                settings_list = batch_list[restore_run:]
            else:
                settings_list = batch_list
                namespace = batch_list[0]["file_namespace"]
                subprocess.run(["mkdir", "-p", f"{OUTPATH}/{namespace}"])
                save_batch(
                    batch_list, f"{OUTPATH}/{namespace}/{namespace}_batch settings.txt"
                )
                logger.info(
                    f"Batch settings saved to {OUTPATH}/{namespace}/{namespace}_batch settings.txt"
                )
            for settings in settings_list:
                setting_string = json.dumps(settings)
                logger.debug("SETTINGS:")
                logger.debug(setting_string)
                params = load_settings(setting_string)
                if params.animation_mode == "3D":
                    init_AdaBins()
                params.allow_overwrite = False
                do_run()
                restore = False
                reencode = False
                gc.collect()
                torch.cuda.empty_cache()
        else:
            do_run()
            logger.info("Complete.")
            gc.collect()
            torch.cuda.empty_cache()
    except KeyboardInterrupt:
        pass
    except RuntimeError:
        print_vram_usage()
        raise


if __name__ == "__main__":
    _main()
