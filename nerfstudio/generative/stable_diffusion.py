# Copyright 2022 The Nerfstudio Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# From https://github.com/ashawkey/stable-dreamfusion/blob/main/nerf/sd.py

import sys
from pathlib import Path
from typing import List, Optional, Union

import matplotlib.pyplot as plt
import numpy as np
import torch
import torch.nn.functional as F
import tyro
from rich.console import Console
from torch import nn
from torchtyping import TensorType

CONSOLE = Console(width=120)

try:
    from diffusers import PNDMScheduler, StableDiffusionPipeline
    from transformers import logging

except ImportError:
    CONSOLE.print("[bold red]Missing Stable Diffusion packages.")
    CONSOLE.print(r"Install using [yellow]pip install nerfstudio\[gen][/yellow]")
    CONSOLE.print(r"or [yellow]pip install -e .\[gen][/yellow] if installing from source.")
    sys.exit(1)

logging.set_verbosity_error()
IMG_DIM = 512
CONST_SCALE = 0.18215

SD_SOURCE = "runwayml/stable-diffusion-v1-5"
CLIP_SOURCE = "openai/clip-vit-large-patch14"


class StableDiffusion(nn.Module):
    """Stable Diffusion implementation"""

    def __init__(self, device) -> None:
        super().__init__()

        self.device = device

        self.num_train_timesteps = 1000
        self.min_step = int(self.num_train_timesteps * 0.02)
        self.max_step = int(self.num_train_timesteps * 0.98)

        self.scheduler = PNDMScheduler(
            beta_start=0.00085,
            beta_end=0.012,
            beta_schedule="scaled_linear",
            num_train_timesteps=self.num_train_timesteps,
        )
        self.alphas = self.scheduler.alphas_cumprod.to(self.device)

        pipe = StableDiffusionPipeline.from_pretrained(SD_SOURCE, torch_dtype=torch.float16)
        assert pipe is not None
        pipe = pipe.to(self.device)

        # MEMORY IMPROVEMENTS
        pipe.enable_attention_slicing()

        # Needs xformers package (installation is from source https://github.com/facebookresearch/xformers)
        # pipe.enable_xformers_memory_efficient_attention()

        # More memory savings for 1/3rd performance
        # pipe.enable_sequential_cpu_offload()

        self.tokenizer = pipe.tokenizer
        self.unet = pipe.unet
        self.text_encoder = pipe.text_encoder
        self.auto_encoder = pipe.vae

        self.unet.to(memory_format=torch.channels_last)

        CONSOLE.print("Stable Diffusion loaded!")

    def get_text_embeds(
        self, prompt: Union[str, List[str]], negative_prompt: Union[str, List[str]]
    ) -> TensorType[2, "max_length", "embed_dim"]:
        """Get text embeddings for prompt and negative prompt

        Args:
            prompt: Prompt text
            negative_prompt: Negative prompt text

        Returns:
            Text embeddings
        """

        # Tokenize text and get embeddings
        text_input = self.tokenizer(
            prompt,
            padding="max_length",
            max_length=self.tokenizer.model_max_length,
            truncation=True,
            return_tensors="pt",
        )

        with torch.no_grad():
            text_embeddings = self.text_encoder(text_input.input_ids.to(self.device))[0]

        # Do the same for unconditional embeddings
        uncond_input = self.tokenizer(
            negative_prompt, padding="max_length", max_length=self.tokenizer.model_max_length, return_tensors="pt"
        )

        with torch.no_grad():
            uncond_embeddings = self.text_encoder(uncond_input.input_ids.to(self.device))[0]

        # Cat for final embeddings
        text_embeddings = torch.cat([uncond_embeddings, text_embeddings])

        return text_embeddings

    def sds_loss(self, text_embeddings, nerf_output, guidance_scale=100):
        nerf_output_np = nerf_output.squeeze(0).permute(1, 2, 0).detach().cpu().numpy()
        nerf_output_np = np.clip(nerf_output_np, 0.0, 1.0)
        plt.imsave("nerf_output.png", nerf_output_np)
        nerf_output = F.interpolate(nerf_output, (IMG_DIM, IMG_DIM), mode="bilinear")
        t = torch.randint(self.min_step, self.max_step + 1, [1], dtype=torch.long, device=self.device)
        latents = self.imgs_to_latent(nerf_output)
        # predict the noise residual with unet, NO grad!
        with torch.no_grad():
            # add noise
            noise = torch.randn_like(latents)
            latents_noisy = self.scheduler.add_noise(latents, noise, t)
            # pred noise
            latent_model_input = torch.cat([latents_noisy] * 2)
            noise_pred = self.unet(latent_model_input, t, encoder_hidden_states=text_embeddings).sample

        # perform guidance (high scale from paper!)
        noise_pred_uncond, noise_pred_text = noise_pred.chunk(2)
        noise_pred = noise_pred_uncond + guidance_scale * (noise_pred_text - noise_pred_uncond)
        # with torch.no_grad():
        #     diffused_img = self.latents_to_img(latents_noisy - noise_pred)
        #     diffused_img = diffused_img.detach().cpu().permute(0, 2, 3, 1).numpy().reshape((512, 512, 3))
        #     diffused_img = (diffused_img * 255).round().astype("uint8")
        #     plt.imsave("sdimg.png", diffused_img)

        # w(t), sigma_t^2
        w = 1 - self.alphas[t]
        # w = self.alphas[t] ** 0.5 * (1 - self.alphas[t])
        grad = w * (noise_pred - noise)

        # clip grad for stable training?
        # grad = grad.clamp(-10, 10)
        grad = torch.nan_to_num(grad)

        # noise_difference = self.latents_to_img(grad)
        # plt.imsave('noise_difference.png', noise_difference.view(3, 512, 512).permute(1, 2, 0).detach().cpu().numpy())

        # manually backward, since we omitted an item in grad and cannot simply autodiff.
        # _t = time.time()

        noise_loss = torch.mean(torch.nan_to_num(torch.square(noise_pred - noise)))

        # print('SDS LOSS:', noise_loss)

        return noise_loss, latents, grad

    def produce_latents(
        self, text_embeddings, height=IMG_DIM, width=IMG_DIM, num_inference_steps=50, guidance_scale=7.5, latents=None
    ):

        if latents is None:
            latents = torch.randn(
                (text_embeddings.shape[0] // 2, self.unet.in_channels, height // 8, width // 8), device=self.device
            )

        self.scheduler.set_timesteps(num_inference_steps)

        with torch.autocast("cuda"):
            for t in self.scheduler.timesteps:
                # expand the latents if we are doing classifier-free guidance to avoid doing two forward passes.
                latent_model_input = torch.cat([latents] * 2)

                # predict the noise residual
                with torch.no_grad():
                    noise_pred = self.unet(latent_model_input, t, encoder_hidden_states=text_embeddings)["sample"]

                # perform guidance
                noise_pred_uncond, noise_pred_text = noise_pred.chunk(2)
                noise_pred = noise_pred_uncond + guidance_scale * (noise_pred_text - noise_pred_uncond)

                # compute the previous noisy sample x_t -> x_t-1
                latents = self.scheduler.step(noise_pred, t, latents)["prev_sample"]

        return latents

    def latents_to_img(self, latents):

        latents = 1 / CONST_SCALE * latents

        with torch.no_grad():
            imgs = self.auto_encoder.decode(latents).sample

        imgs = (imgs / 2 + 0.5).clamp(0, 1)

        return imgs

    def imgs_to_latent(self, imgs):
        # imgs: [B, 3, H, W]
        imgs = 2 * imgs - 1

        posterior = self.auto_encoder.encode(imgs).latent_dist
        latents = posterior.sample() * CONST_SCALE

        return latents

    def prompt_to_img(
        self,
        prompts: Union[str, List[str]],
        negative_prompts: Union[str, List[str]] = "",
        num_inference_steps: int = 50,
        guidance_scale: float = 7.5,
        latents=None,
    ) -> np.ndarray:
        """Generate an images from a prompts.

        Args:
            prompts: The prompt to generate an image from.
            negative_prompts: The negative prompt to generate an image from.
            num_inference_steps: The number of inference steps to perform.
            guidance_scale: The scale of the guidance.
            latents: The latents to start from, defaults to random.

        Returns:
            The generated image.
        """

        prompts = [prompts] if isinstance(prompts, str) else prompts
        negative_prompts = [negative_prompts] if isinstance(negative_prompts, str) else negative_prompts
        text_embeddings = self.get_text_embeds(prompts, negative_prompts)
        latents = self.produce_latents(
            text_embeddings,
            height=IMG_DIM,
            width=IMG_DIM,
            latents=latents,
            num_inference_steps=num_inference_steps,
            guidance_scale=guidance_scale,
        )  # [1, 4, resolution, resolution]

        diffused_img = self.latents_to_img(latents.half())
        diffused_img = diffused_img.detach().cpu().permute(0, 2, 3, 1).numpy()
        diffused_img = (diffused_img * 255).round().astype("uint8")

        return diffused_img

    def forward(
        self, prompts, negative_prompts="", num_inference_steps=50, guidance_scale=7.5, latents=None
    ) -> np.ndarray:
        """Generate an image from a prompt.

        Args:
            prompts: The prompt to generate an image from.
            negative_prompts: The negative prompt to generate an image from.
            num_inference_steps: The number of inference steps to perform.
            guidance_scale: The scale of the guidance.
            latents: The latents to start from, defaults to random.

        Returns:
            The generated image.
        """
        return self.prompt_to_img(prompts, negative_prompts, num_inference_steps, guidance_scale, latents)


def generate_image(
    prompt: str, negative: str = "", seed: int = 0, steps: int = 50, save_path: Path = Path("test_sd.png")
):
    """Generate an image from a prompt using Stable Diffusion.

    Args:
        prompt: The prompt to use.
        negative: The negative prompt to use.
        seed: The random seed to use.
        steps: The number of steps to use.
        save_path: The path to save the image to.
    """
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    cuda_device = torch.device("cuda")
    with torch.no_grad():
        sd = StableDiffusion(cuda_device)
        imgs = sd.prompt_to_img(prompt, negative, steps)
        plt.imsave(str(save_path), imgs[0])


if __name__ == "__main__":
    tyro.cli(generate_image)
