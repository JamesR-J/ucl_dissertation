import sys

import flax.linen as nn
import functools
import jax.numpy as jnp
import jax.random as jrandom
import numpy as np
from flax.linen.initializers import constant, orthogonal
from typing import Sequence, NamedTuple, Any, Dict
import distrax


class ActorCriticRNN(nn.Module):
    action_dim: Sequence[int]
    activation: str = "tanh"
    config: Dict

    @nn.compact
    def __call__(self, hidden, x):
        if self.activation == "relu":
            activation = nn.relu
        else:
            activation = nn.tanh

        obs, dones = x
        embedding = nn.Dense(128, kernel_init=orthogonal(np.sqrt(2)), bias_init=constant(0.0))(obs)
        embedding = activation(embedding)
        embedding = nn.Dense(128, kernel_init=orthogonal(np.sqrt(2)), bias_init=constant(0.0))(embedding)
        embedding = activation(embedding)
        actor_mean = nn.Dense(self.action_dim, kernel_init=orthogonal(0.01), bias_init=constant(0.0))(embedding)

        pi = distrax.Categorical(logits=actor_mean)

        critic = nn.Dense(128, kernel_init=orthogonal(np.sqrt(2)), bias_init=constant(0.0))(obs)
        critic = activation(critic)
        critic = nn.Dense(128, kernel_init=orthogonal(np.sqrt(2)), bias_init=constant(0.0))(critic)
        critic = activation(critic)
        critic = nn.Dense(1, kernel_init=orthogonal(1.0), bias_init=constant(0.0))(critic)

        return hidden, pi, jnp.squeeze(critic, axis=-1)