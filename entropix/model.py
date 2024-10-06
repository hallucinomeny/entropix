from typing import Optional, Tuple
import jax
import jax.numpy as jnp
from entropix.config import ModelParams
from entropix.kvcache import KVCache
from entropix.weights import XfmrWeights, LayerWeights
from entropix.rope import apply_rotary_emb
from entropix.lm_state import LMState
from entropix.rope import precompute_freqs_cis

DEFAULT_MASK_VALUE = -0.7 * float(jnp.finfo(jnp.dtype("float32")).max)

#@partial(jax.jit, static_argnames=("eps"))
def rms_norm(x: jax.Array, w: jax.Array, eps: float = 1e-6) -> jax.Array:
  return w * (x * jax.lax.rsqrt(jax.lax.pow(x, 2).mean(-1, keepdims=True) + eps))

#@partial(jax.jit, static_argnames=("model_params", "cur_pos", "layer_idx"))
def attention(x: jax.Array, layer_weights: LayerWeights, model_params, cur_pos: int, layer_idx: int, freqs_cis: jax.Array, kvcache: KVCache, attn_mask: Optional[jax.Array] = None) -> Tuple[jax.Array, KVCache]:
  bsz, _, _ = x.shape
  n_rep = model_params.n_local_heads // model_params.n_local_kv_heads
  xq = jnp.dot(x, layer_weights.wq.T).reshape(bsz, -1, model_params.n_local_heads, model_params.head_dim)
  xk = jnp.dot(x, layer_weights.wk.T).reshape(bsz, -1, model_params.n_local_kv_heads, model_params.head_dim)
  xv = jnp.dot(x, layer_weights.wv.T).reshape(bsz, -1, model_params.n_local_kv_heads, model_params.head_dim)
  xq, xk = apply_rotary_emb(xq, xk, freqs_cis=freqs_cis)
  keys, values, kvcache = kvcache.update(xk, xv, layer_idx, cur_pos, n_rep)
  xq = jnp.transpose(xq, (0, 2, 1, 3))  # (bs, n_heads, seqlen, head_dim)
  keys = jnp.transpose(keys, (0, 2, 3, 1))  # (bs, n_heads, head_dim, cache_len + seqlen)
  values = jnp.transpose(values, (0, 2, 1, 3))  # (bs, n_heads, cache_len + seqlen, head_dim)
  scores = jnp.matmul(xq, keys)
  pre_scores = scores / jnp.sqrt(model_params.head_dim)
  scores = pre_scores.astype(jnp.float32)  # Always do attention softmax at float32
  if cur_pos == 0: # not batched currently
    scores = scores + attn_mask
  mask = jnp.where(scores != 0.0, scores, DEFAULT_MASK_VALUE)
  padded_logits = jnp.where((mask >= DEFAULT_MASK_VALUE * 0.5), scores, DEFAULT_MASK_VALUE)
  scores = jax.nn.softmax(padded_logits, axis=-1).astype(x.dtype)
  output = jnp.matmul(scores, values)
  output = jnp.swapaxes(output, 1, 2).reshape(xq.shape[0], xq.shape[2], -1)
  out = jnp.dot(output, layer_weights.wo.T)
  return out, kvcache, pre_scores

#@partial(jax.jit)
def feed_forward(x: jax.Array, layer_weights: LayerWeights) -> jax.Array:
 return jnp.dot(jax.nn.silu(jnp.dot(x, layer_weights.w1.T)) * jnp.dot(x, layer_weights.w3.T), layer_weights.w2.T)

#@partial(jax.jit, static_argnames=("model_params", "cur_pos"))
def xfmr(xfmr_weights: XfmrWeights, model_params: ModelParams, lm_state: LMState, kvcache: KVCache, attn_mask: Optional[jax.Array]=None) -> Tuple[jax.Array, KVCache]:
  freqs_cis = precompute_freqs_cis(model_params)
  if kvcache.is_new:
    
    freqs_cis = freqs_cis[:lm_state.start_pos]
    h = xfmr_weights.tok_embeddings[lm_state.context[:,:lm_state.start_pos]]
  else:
    freqs_cis = freqs_cis[lm_state.cur_pos:lm_state.cur_pos+1]
    h = xfmr_weights.tok_embeddings[lm_state.context[:,lm_state.cur_pos:lm_state.cur_pos+1]]
  for i in range(model_params.n_layers):
    norm_x = rms_norm(h, xfmr_weights.layer_weights[i].attention_norm)
    h_attn, kvcache, scores = attention(norm_x, xfmr_weights.layer_weights[i], model_params, lm_state.cur_pos, i, freqs_cis, kvcache, attn_mask=attn_mask)
    lm_state = lm_state.update_attn_stats(scores, i)
    h = h + h_attn
    h = h + feed_forward(rms_norm(h, xfmr_weights.layer_weights[i].ffn_norm), xfmr_weights.layer_weights[i])
  logits = jnp.dot(rms_norm(h, xfmr_weights.norm), xfmr_weights.output.T)
  return logits, kvcache, lm_state, scores


