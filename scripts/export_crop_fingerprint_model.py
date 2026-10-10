"""Rebuild the crop fingerprint model used by src/services/crop_fingerprints.py (run on a machine with
`transformers`; the app itself only needs torch).

    python scripts/export_crop_fingerprint_model.py [squares_dir]

Writes dinov2_small_fingerprint.pt: TorchScript of facebook/dinov2-small at a pinned revision, taking
(N, 3, 224, 224) normalised RGB and returning (N, 768) = [CLS token, mean of patch tokens], L2-normalised. With a
folder of square PNGs it also checks that the app's own preprocessing (crop_fingerprints._prepare) gives the
same fingerprints as DINOv2's image processor. Upload the file to the app's bucket at
crop_fingerprints.MODEL_KEY and set MODEL_SHA256 to the sha256 printed here.
"""

from __future__ import annotations

import glob
import hashlib
import sys

import numpy as np
import torch
from PIL import Image
from transformers import AutoImageProcessor, AutoModel

REVISION = "ed25f3a31f01632728cabb09d1542f84ab7b0056"  # facebook/dinov2-small, 2026-10-10
OUT = "dinov2_small_fingerprint.pt"


class Fingerprint(torch.nn.Module):
    def __init__(self, model: torch.nn.Module) -> None:
        super().__init__()
        self.model = model

    def forward(self, pixels: torch.Tensor) -> torch.Tensor:
        hidden = self.model(pixel_values=pixels).last_hidden_state
        return torch.nn.functional.normalize(torch.cat([hidden[:, 0], hidden[:, 1:].mean(1)], 1), dim=1)


def main() -> None:
    model = Fingerprint(AutoModel.from_pretrained("facebook/dinov2-small", revision=REVISION).eval()).eval()
    torch.jit.trace(model, torch.randn(2, 3, 224, 224), check_trace=False).save(OUT)
    print("sha256", hashlib.sha256(open(OUT, "rb").read()).hexdigest())
    if len(sys.argv) < 2:
        return
    sys.path.insert(0, ".")
    from src.services.crop_fingerprints import _prepare  # noqa: E402 - only for the check

    images = [Image.open(f).convert("RGB") for f in sorted(glob.glob(f"{sys.argv[1]}/*.png"))[:16]]
    processor = AutoImageProcessor.from_pretrained("facebook/dinov2-small", revision=REVISION)
    scripted = torch.jit.load(OUT)
    with torch.no_grad():
        theirs = model(processor(images=images, return_tensors="pt")["pixel_values"]).numpy()
        ours = scripted(torch.from_numpy(np.stack([_prepare(np.asarray(i)) for i in images]))).numpy()
    print("max |difference|", float(np.abs(theirs - ours).max()), "min cosine", float((theirs * ours).sum(1).min()))


if __name__ == "__main__":
    main()
