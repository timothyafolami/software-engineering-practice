"""
Layer 10 - Topic 7: a decoder-only transformer, written out. (MLX)

What this file is
    No framework attention, no fused kernels, no `nn.MultiHeadAttention`.
    RoPE, the attention itself, RMSNorm, SwiGLU and AdamW are all here in
    a form you can read line by line, because the point of the topic is
    that the pieces stop being magic once you have typed them.

    Two attention implementations are provided and selected by name:

      naive  exp(scores) / sum(exp(scores)).  Correct algebra. Overflows
             the moment max(scores) passes the format's exp range -- about
             11.09 in float16, because exp(11.09) is 65500 and float16
             stops at 65504.
      lse    exp(scores - max(scores)) / sum(exp(scores - max(scores))).
             EXACT, not an approximation: multiplying numerator and
             denominator by exp(-max) is an identity. One subtraction.

    Train with `--softmax naive --dtype float16` and watch the loss become
    nan; switch one word and watch it train. That contrast is topic 4's
    finding arriving inside your own model.

What to look for
    - `param_count` and the 6ND FLOP estimate agreeing with what mfu.py
      computes independently. Two derivations of the same number that
      agree is how you know neither is a typo.
    - The causal mask being additive (-inf on masked positions before the
      softmax) rather than multiplicative afterwards. Multiplying after
      the softmax renormalises over positions the model should not have
      seen -- a bug that trains fine and cheats.
    - AdamW's decoupled weight decay: it is applied to the PARAMETER, not
      added to the gradient. Adding it to the gradient is Adam+L2, which
      is a different algorithm with a different optimum, and the two get
      confused constantly.

Imported by train.py. Runs standalone as a shape-and-gradient self-check:
    python3 python/model.py
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import mlx.core as mx
import mlx.nn as nn


@dataclass
class Config:
    vocab_size: int = 256          # byte-level: no tokenizer to get wrong
    n_layers: int = 6
    n_heads: int = 8
    d_model: int = 384
    d_ff: int | None = None        # defaults to 8/3 * d_model, SwiGLU convention
    max_seq: int = 512
    rope_theta: float = 10_000.0
    softmax: str = "lse"           # "lse" | "naive"
    # Storage precision and ACCUMULATION precision are separate decisions.
    # Parameters and the optimizer stay float32 here (which is what mixed
    # precision does in practice); this is the dtype the attention scores
    # and the softmax are computed in.
    attn_dtype: str = "float32"    # "float32" | "float16" | "bfloat16"
    # Multiplies the pre-softmax scores. Stands in for the score growth
    # that happens naturally deep in training and at larger d_model, so the
    # overflow can be produced in a two-minute run instead of a two-day
    # one. It is a knob, labelled as a knob -- not a result.
    logit_scale: float = 1.0

    def __post_init__(self):
        if self.d_ff is None:
            # SwiGLU has three matrices instead of two, so the usual 4x is
            # scaled by 2/3 to keep the parameter count comparable.
            self.d_ff = int(8 * self.d_model / 3 / 64 + 0.5) * 64
        assert self.d_model % self.n_heads == 0

    @property
    def head_dim(self) -> int:
        return self.d_model // self.n_heads


def rope_frequencies(head_dim: int, seq: int, theta: float, dtype) -> tuple:
    """Rotary position embeddings, precomputed as (cos, sin).

    RoPE rotates each 2-dimensional slice of a head by an angle
    proportional to the position, so the dot product between a query at
    position m and a key at position n depends only on (m - n). That is
    the whole idea: relative position falls out of the geometry instead of
    being added as a learned vector.
    """
    inv_freq = 1.0 / (theta ** (mx.arange(0, head_dim, 2, dtype=mx.float32) / head_dim))
    pos = mx.arange(seq, dtype=mx.float32)
    angles = pos[:, None] * inv_freq[None, :]            # (seq, head_dim/2)
    return mx.cos(angles).astype(dtype), mx.sin(angles).astype(dtype)


def apply_rope(x: mx.array, cos: mx.array, sin: mx.array) -> mx.array:
    """x: (batch, heads, seq, head_dim). Rotates adjacent pairs."""
    x1 = x[..., 0::2]
    x2 = x[..., 1::2]
    cos = cos[None, None, : x.shape[2], :]
    sin = sin[None, None, : x.shape[2], :]
    rotated = mx.stack([x1 * cos - x2 * sin, x1 * sin + x2 * cos], axis=-1)
    return rotated.reshape(x.shape)


def softmax_naive(scores: mx.array) -> mx.array:
    """The version in the textbook. Overflows, and says nothing when it does."""
    e = mx.exp(scores)
    return e / mx.sum(e, axis=-1, keepdims=True)


def softmax_lse(scores: mx.array) -> mx.array:
    """The same function, arranged so the largest exponent is exp(0) = 1."""
    m = mx.max(scores, axis=-1, keepdims=True)
    e = mx.exp(scores - m)
    return e / mx.sum(e, axis=-1, keepdims=True)


class Attention(nn.Module):
    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg
        # Separate projections on purpose: fusing them into one matrix is
        # the "make exactly one change you expect to help" experiment in
        # the topic README, and you cannot measure a fusion you started
        # with.
        self.wq = nn.Linear(cfg.d_model, cfg.d_model, bias=False)
        self.wk = nn.Linear(cfg.d_model, cfg.d_model, bias=False)
        self.wv = nn.Linear(cfg.d_model, cfg.d_model, bias=False)
        self.wo = nn.Linear(cfg.d_model, cfg.d_model, bias=False)

    def __call__(self, x: mx.array, cos, sin, mask) -> mx.array:
        b, t, _ = x.shape
        h, d = self.cfg.n_heads, self.cfg.head_dim

        q = self.wq(x).reshape(b, t, h, d).transpose(0, 2, 1, 3)
        k = self.wk(x).reshape(b, t, h, d).transpose(0, 2, 1, 3)
        v = self.wv(x).reshape(b, t, h, d).transpose(0, 2, 1, 3)

        q = apply_rope(q, cos, sin)
        k = apply_rope(k, cos, sin)

        scores = (q @ k.transpose(0, 1, 3, 2)) / math.sqrt(d)
        scores = scores * self.cfg.logit_scale
        # Additive mask, BEFORE the softmax. A multiplicative mask applied
        # afterwards renormalises over positions the model should not have
        # been able to see, which trains fine and cheats.
        scores = scores + mask[:t, :t]

        compute_dtype = {"float32": mx.float32, "float16": mx.float16,
                         "bfloat16": mx.bfloat16}[self.cfg.attn_dtype]
        scores = scores.astype(compute_dtype)
        attn = (softmax_naive(scores) if self.cfg.softmax == "naive"
                else softmax_lse(scores))
        attn = attn.astype(v.dtype)
        out = (attn @ v).transpose(0, 2, 1, 3).reshape(b, t, h * d)
        return self.wo(out)


class SwiGLU(nn.Module):
    def __init__(self, cfg: Config):
        super().__init__()
        self.w1 = nn.Linear(cfg.d_model, cfg.d_ff, bias=False)
        self.w2 = nn.Linear(cfg.d_ff, cfg.d_model, bias=False)
        self.w3 = nn.Linear(cfg.d_model, cfg.d_ff, bias=False)

    def __call__(self, x: mx.array) -> mx.array:
        return self.w2(nn.silu(self.w1(x)) * self.w3(x))


class Block(nn.Module):
    def __init__(self, cfg: Config):
        super().__init__()
        self.norm1 = nn.RMSNorm(cfg.d_model)
        self.attn = Attention(cfg)
        self.norm2 = nn.RMSNorm(cfg.d_model)
        self.mlp = SwiGLU(cfg)

    def __call__(self, x, cos, sin, mask):
        # Pre-norm residual: the residual stream is never normalised, which
        # is what keeps gradients flowing through depth. Post-norm needs a
        # warmup schedule to train at all at this depth.
        x = x + self.attn(self.norm1(x), cos, sin, mask)
        return x + self.mlp(self.norm2(x))


class Transformer(nn.Module):
    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg
        self.embed = nn.Embedding(cfg.vocab_size, cfg.d_model)
        self.blocks = [Block(cfg) for _ in range(cfg.n_layers)]
        self.norm = nn.RMSNorm(cfg.d_model)
        self.out = nn.Linear(cfg.d_model, cfg.vocab_size, bias=False)

    def __call__(self, idx: mx.array) -> mx.array:
        b, t = idx.shape
        x = self.embed(idx)
        cos, sin = rope_frequencies(self.cfg.head_dim, t, self.cfg.rope_theta, x.dtype)
        mask = mx.triu(mx.full((t, t), -1e9, dtype=x.dtype), k=1)
        for block in self.blocks:
            x = block(x, cos, sin, mask)
        return self.out(self.norm(x))


def cross_entropy_loss(model: Transformer, inputs: mx.array,
                       targets: mx.array) -> mx.array:
    logits = model(inputs).astype(mx.float32)
    return mx.mean(nn.losses.cross_entropy(logits.reshape(-1, logits.shape[-1]),
                                           targets.reshape(-1)))


def param_count(model: Transformer) -> int:
    from mlx.utils import tree_flatten
    return sum(v.size for _, v in tree_flatten(model.parameters()))


def flops_per_token(n_params: int) -> int:
    """The 6ND rule: roughly 6 FLOPs per parameter per token of training.

    2 for the forward multiply-accumulate, 4 for the backward pass, which
    computes gradients with respect to both the inputs and the weights.
    It ignores attention's quadratic term, which is a good approximation
    while seq << d_model * layers and a bad one at long context -- mfu.py
    reports both so you can see when the approximation stops holding.
    """
    return 6 * n_params


class AdamW:
    """Decoupled weight decay, written out.

    The decay is applied to the PARAMETER, not added to the gradient.
    Adding it to the gradient is Adam+L2, a different algorithm with a
    different optimum, and the two are confused constantly -- including in
    the original Adam paper's own implementations, which is what prompted
    Loshchilov & Hutter to name the distinction.
    """

    def __init__(self, lr=3e-4, betas=(0.9, 0.95), eps=1e-8, weight_decay=0.1):
        self.lr = lr
        self.b1, self.b2 = betas
        self.eps = eps
        self.wd = weight_decay
        self.t = 0
        self.m: dict = {}
        self.v: dict = {}

    def update(self, params: dict, grads: dict, lr: float | None = None) -> dict:
        self.t += 1
        lr = self.lr if lr is None else lr
        # Bias correction: m and v start at zero, so early steps are biased
        # toward zero and need dividing by (1 - beta^t).
        bc1 = 1 - self.b1 ** self.t
        bc2 = 1 - self.b2 ** self.t

        def step(path, p, g):
            m = self.m.get(path, mx.zeros_like(p))
            v = self.v.get(path, mx.zeros_like(p))
            m = self.b1 * m + (1 - self.b1) * g
            v = self.b2 * v + (1 - self.b2) * mx.square(g)
            self.m[path], self.v[path] = m, v
            update = (m / bc1) / (mx.sqrt(v / bc2) + self.eps)
            # Decoupled: the decay term never touches m or v.
            return p - lr * (update + self.wd * p)

        def walk(prefix, ps, gs):
            if isinstance(ps, dict):
                return {k: walk(f"{prefix}.{k}", v, gs[k]) for k, v in ps.items()}
            if isinstance(ps, list):
                return [walk(f"{prefix}.{i}", v, gs[i]) for i, v in enumerate(ps)]
            return step(prefix, ps, gs)

        return walk("", params, grads)


def _self_check() -> None:
    cfg = Config(n_layers=2, n_heads=4, d_model=128, max_seq=64)
    model = Transformer(cfg)
    mx.eval(model.parameters())
    n = param_count(model)

    idx = mx.array([[1, 2, 3, 4, 5, 6, 7, 8]])
    logits = model(idx)
    mx.eval(logits)

    print("model.py self-check")
    print(f"  parameters            : {n:,}")
    print(f"  logits shape          : {logits.shape}  "
          f"(batch, seq, vocab) -- expected (1, 8, {cfg.vocab_size})")
    print(f"  6ND FLOPs per token   : {flops_per_token(n):,}")

    # Causality: changing a LATER token must not change an EARLIER logit.
    idx2 = mx.array([[1, 2, 3, 4, 5, 6, 7, 99]])
    l2 = model(idx2)
    mx.eval(l2)
    early_same = bool(mx.all(mx.abs(logits[0, :7] - l2[0, :7]) < 1e-5))
    late_diff = bool(mx.any(mx.abs(logits[0, 7] - l2[0, 7]) > 1e-6))
    print(f"  causal mask holds     : {'PASS' if early_same else 'FAIL'} "
          f"(earlier logits unchanged when a later token changes)")
    print(f"  last position reacts  : {'PASS' if late_diff else 'FAIL'}")

    # RoPE is RELATIVE: shifting a query and a key by the same amount must
    # leave their dot product unchanged. That is the property the whole
    # scheme exists for, so it is worth asserting rather than believing.
    cos, sin = rope_frequencies(cfg.head_dim, 32, cfg.rope_theta, mx.float32)
    q = mx.random.normal((1, 1, 1, cfg.head_dim))
    k = mx.random.normal((1, 1, 1, cfg.head_dim))

    def rotate_at(vec, pos):
        c = cos[pos:pos + 1, :]
        s_ = sin[pos:pos + 1, :]
        return apply_rope(vec, c, s_)

    dots = []
    for shift in (0, 3, 9):
        rq = rotate_at(q, 2 + shift)
        rk = rotate_at(k, 5 + shift)
        dots.append(float(mx.sum(rq * rk)))
    spread = max(dots) - min(dots)
    print(f"  RoPE relative         : {'PASS' if spread < 1e-4 else 'FAIL'}  "
          f"q·k at offsets (2,5), (5,8), (11,14) = "
          f"{', '.join(f'{d:+.5f}' for d in dots)}")
    print("                          identical because the rotation depends only")
    print("                          on (m - n), which is the entire point of RoPE")

    # The two softmaxes agree in float32 and diverge in float16.
    scores = mx.array([[0.0, 5.0, 12.0]])
    for dtype in (mx.float32, mx.float16):
        s = scores.astype(dtype)
        a, b = softmax_naive(s), softmax_lse(s)
        mx.eval(a, b)
        print(f"  softmax {str(dtype).split('.')[-1]:<8} naive={[round(float(x), 4) for x in a[0]]} "
              f"lse={[round(float(x), 4) for x in b[0]]}")
    print("  In float16 exp(12) is 162754, past the format's 65504 ceiling, so")
    print("  the naive form returns inf/inf. The lse form never exponentiates")
    print("  anything above zero.")


if __name__ == "__main__":
    _self_check()
