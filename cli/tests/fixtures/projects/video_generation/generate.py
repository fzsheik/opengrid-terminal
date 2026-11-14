import torch
from diffusers import CogVideoXPipeline
from diffusers.utils import export_to_video

pipe = CogVideoXPipeline.from_pretrained("THUDM/CogVideoX-5b", torch_dtype=torch.bfloat16).to("cuda")

video = pipe(prompt="a panda playing guitar in a bamboo forest", num_inference_steps=50, num_frames=49,
             guidance_scale=6).frames[0]
export_to_video(video, "out.mp4", fps=8)
