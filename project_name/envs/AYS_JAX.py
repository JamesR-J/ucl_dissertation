"""
This is the implementation of the AYS Environment in the form
that it can used within the Agent-Environment interface
in combination with the DRL-agent.

@author: Felix Strnad, Theodore Wolf

"""

import sys
import torch
import os

import numpy as np
import matplotlib.pyplot as plt

from scipy.integrate import odeint
from gym import Env
from enum import Enum
from inspect import currentframe, getframeinfo

from .graph_functions import create_figure_ays
from . import graph_functions as ays_plot, ays_model as ays
from flax import struct
import chex
import jax
import jax.numpy as jnp
import jax.random as jrandom
from functools import partial


# class Basins(Enum):
#     OUT_PB = 0
#     BLACK_FP = 1
#     GREEN_FP = 2
#
#     A_PB = 3
#     Y_SF = 4
#     E_PB = 5
#
#     OUT_OF_TIME = 6


@struct.dataclass
class EnvState:  # TODO fill this env state up as we need it basos
    pass

class AYS_Environment(object):
    dimensions = np.array(['A', 'Y', 'S'])
    management_options = ['default', 'LG', 'ET', 'LG+ET']
    action_space = torch.tensor([[False, False], [True, False], [False, True], [True, True]])
    action_space_number = np.arange(len(action_space))

    tau_A = 50  # carbon decay - single val
    tau_S = 50  # renewable knowledge stock decay - multi val
    beta = 0.03  # economic output growth - multi val
    beta_LG = 0.015  # halved economic output growth - multi val
    eps = 147  # energy efficiency param - single val
    A_offset = 600
    theta = beta / (950 - A_offset)  # beta / ( 950 - A_offset(=350) )
    # theta = 8.57e-5

    rho = 2.  # renewable knowledge learning rate - multi val
    sigma = 4e12  # break even knowledge - multi val
    sigma_ET = sigma * 0.5 ** (1 / rho)  # can't remember the change, but it's somewhere - multi val

    phi = 4.7e10

    trade = 1.0
    trade_inflicted = 1.4

    possible_test_cases = [[0.4949063922255394, 0.4859623171738628, 0.5], [0.42610779, 0.52056811, 0.5]]

    def __init__(self, gamma=0.99, t0=0, dt=1, reward_type='PB', max_steps=600, image_dir='./images/', run_number=0,
                 plot_progress=False, num_agents=2, obs_type='all_agents', trade_actions=False, homogeneous=False):
        self.management_cost = 0.5
        self.image_dir = image_dir
        self.run_number = run_number
        self.plot_progress = plot_progress
        self.max_steps = max_steps
        self.gamma = gamma

        self.num_agents = num_agents
        self.tau_A = torch.tensor([self.tau_A]).repeat(self.num_agents, 1)
        self.tau_S = torch.tensor([self.tau_S]).repeat(self.num_agents, 1)
        self.beta = torch.tensor([self.beta]).repeat(self.num_agents, 1)
        self.beta_LG = torch.tensor([self.beta_LG]).repeat(self.num_agents, 1)
        self.eps = torch.tensor([self.eps]).repeat(self.num_agents, 1)
        self.theta = torch.tensor([self.theta]).repeat(self.num_agents, 1)
        self.rho = torch.tensor([self.rho]).repeat(self.num_agents, 1)
        self.sigma = torch.tensor([self.sigma]).repeat(self.num_agents, 1)
        self.sigma_ET = torch.tensor([self.sigma_ET]).repeat(self.num_agents, 1)
        self.phi = torch.tensor([self.phi]).repeat(self.num_agents, 1)

        # The grid defines the number of cells, hence we have 8x8 possible states
        self.final_state = torch.tensor([False]).repeat(self.num_agents, 1)
        self.reward = torch.tensor([0.0]).repeat(self.num_agents, 1)
        self.reward_type = reward_type
        print(f"Reward type: {self.reward_type}")

        self.timeStart = 0
        self.intSteps = 10  # integration Steps
        self.t = self.t0 = t0
        self.dt = dt

        self.sim_time_step = np.linspace(self.timeStart, dt, self.intSteps)

        self.green_fp = torch.tensor([0, 1, 0]).repeat(self.num_agents, 1)
        self.brown_fp = torch.tensor([1, 0.4, 0.6]).repeat(self.num_agents, 1)
        self.final_radius = torch.tensor([0.05]).repeat(self.num_agents, 1)
        self.color_list = ays_plot.color_list

        self.X_MID = [240, 7e13, 501.5198]

        self.trade_actions = trade_actions

        if self.trade_actions:
            self.action_space = torch.tensor(
                [[False, False, False], [True, False, False], [False, True, False], [True, True, False],
                 [False, False, True], [False, True, True], [True, False, True], [True, True, True]])
            self.action_space_number = np.arange(len(self.action_space))

        self.state = self.current_state = torch.tensor([0.5, 0.5, 0.5]).repeat(self.num_agents, 1)
        self.reward_space = torch.tensor([0.5, 0.5, 0.5, 10.0 / 1003.04]).repeat(self.num_agents, 1)
        self.obs_type = obs_type
        print(f"Observation type: {self.obs_type}")
        if self.obs_type == 'agent_only':
            self.observation_space = torch.tensor([0.5, 0.5, 0.5, 10.0 / 20]).repeat(self.num_agents, 1)
        elif self.obs_type == 'all_shared' and not self.trade_actions:
            self.observation_space = torch.cat((torch.eye(self.num_agents), torch.tensor([0.5]).repeat(self.num_agents, 1), torch.tensor([0.5, 0.5, 10.0 / 20] * self.num_agents).repeat(self.num_agents, 1)), dim=1)
        elif self.obs_type == "all_shared" and self.trade_actions:
            self.observation_space = torch.cat((torch.eye(self.num_agents), torch.tensor([0.5]).repeat(self.num_agents, 1), torch.tensor([0.5, 0.5, 10.0 / 20] * self.num_agents).repeat(self.num_agents, 1), torch.tensor([0.0] * self.num_agents).repeat(self.num_agents, 1)), dim=1)

        """
        This values define the planetary boundaries of the AYS model
        """
        self.A_PB = torch.tensor([self._compactification(ays.boundary_parameters["A_PB"], self.X_MID[0])]).repeat(self.num_agents, 1)  # Planetary boundary: 0.5897
        self.Y_SF = torch.tensor([self._compactification(ays.boundary_parameters["W_SF"], self.X_MID[1])]).repeat(self.num_agents, 1)  # Social foundations as boundary: 0.3636
        self.E_LIMIT = torch.tensor([1.0]).repeat(self.num_agents, 1)
        self.PB = torch.cat((self.E_LIMIT,
                             self.Y_SF,
                             self.A_PB), dim=1)  # EYA
        self.PB_2 = torch.cat((self.A_PB,
                               self.Y_SF,
                               torch.tensor([0.0]).repeat(self.num_agents, 1)), dim=1)  # AYS
        self.PB_3 = torch.cat((torch.tensor([0.0]).repeat(self.num_agents, 1),
                               self.Y_SF,
                               torch.tensor([0.0]).repeat(self.num_agents, 1),
                               torch.tensor([0.0]).repeat(self.num_agents, 1)), dim=1)  # AYSE
        self.PB_4 = torch.cat((self.A_PB,
                               torch.tensor([0.0]).repeat(self.num_agents, 1),
                               torch.tensor([0.0]).repeat(self.num_agents, 1)), dim=1)  # AYS

        self.old_reward = torch.tensor([0.0]).repeat(self.num_agents, 1)

        self.homogeneous = homogeneous

    @partial(jax.jit, static_argnums=(0,))
    def reset(self, key: chex.PRNGKey):
        if start_state is None:
            self.state = self.current_state_region_StartPoint()
        else:
            self.state = start_state

        self.final_state = torch.tensor([False]).repeat(self.num_agents, 1)
        self.t = self.t0

        self.reward_space = torch.cat((self.state, torch.tensor(10.0 / 20).repeat(self.num_agents, 1)),
                                      dim=1)  # bit dodgy assuming emissions at 10, then normalised

        self.old_reward = torch.tensor([0.0]).repeat(self.num_agents, 1)

        if self.obs_type == 'agent_only':
            self.observation_space = torch.cat((self.state, torch.tensor(10.0 / 20).repeat(self.num_agents, 1)),
                                               dim=1)  # bit dodgy assuming emissions at 10, then normalised
        elif self.obs_type == 'all_shared' and not self.trade_actions:
            mid = torch.cat((self.state[:, 1:], torch.tensor([10.0 / 20]).repeat(self.num_agents, 1)),
                            dim=1).flatten().repeat(self.num_agents, 1)
            self.observation_space = torch.cat(
                (torch.eye(self.num_agents), self.state[:, 0].view(self.num_agents, 1), mid), dim=1)
        elif self.obs_type == 'all_shared' and self.trade_actions:
            mid = torch.cat((self.state[:, 1:], torch.tensor([10.0 / 20]).repeat(self.num_agents, 1)),
                            dim=1).flatten().repeat(self.num_agents, 1)
            self.observation_space = torch.cat((torch.eye(self.num_agents), self.state[:, 0].view(self.num_agents, 1),
                                                mid, torch.tensor([0.0] * self.num_agents).repeat(self.num_agents, 1)),
                                               dim=1)

        return self.state.clone(), self.observation_space

    def step_env(self, action: int):

        next_t = self.t + self.dt

        result, parameter_matrix = self._perform_step(action, next_t)  # A Y S E
        self.state[:, 0] = result[:, 3].clone()
        self.state[:, 1] = result[:, 1].clone()
        self.state[:, 2] = result[:, 0].clone()
        self.observation_space, self.reward_space = self.generate_observation(result, parameter_matrix)

        if not self.final_state.bool().any():
            assert torch.all(self.state[:, 2] == self.state[0, 2]), "Values in the A column are not all equal"

        self.t = next_t

        self.get_reward_function(action)

        for agent in range(self.num_agents):
            if self._arrived_at_final_state(agent):
                self.final_state[agent] = True
            if self.reward_type[agent] == "PB":  #  or self.reward_type[agent] == "PB_new_new_new_new":
                if not self._inside_planetary_boundaries(agent):
                    self.final_state[agent] = True
            else:
                if not self._inside_A_pb(agent):
                    self.final_state[agent] = True

        # if not self.trade_actions:  # if using trade actions then this does not apply as the reward functions may not use same definition of reaching a final state  # this is the One Stop strategy
        #     if torch.any(self.final_state):
        #         for agent in range(self.num_agents):
        #             if self.final_state[agent]:
        #                 if self.green_fixed_point(agent):
        #                     pass
        #                 else:
        #                     self.final_state = torch.tensor([True]).repeat(self.num_agents, 1)

        if torch.all(self.final_state):
            for agent in range(self.num_agents):
                if self.trade_actions:
                    if self.reward_type[agent] == "PB" or self.reward_type[agent] == "IPB":
                        e = self.state[agent, 0]
                        y = self.state[agent, 1]
                        a = self.state[agent, 2]
                        if torch.abs(e - self.green_fp[agent, 0]) < self.final_radius[agent] \
                                and torch.abs(y - self.green_fp[agent, 1]) < self.final_radius[agent] \
                                and torch.abs(a - self.green_fp[agent, 2]) < self.final_radius[agent]:
                            self.reward[agent] += 10

                self.reward[agent] += self.calculate_expected_final_reward(agent)
        else:
            self.final_state = torch.tensor([False]).repeat(self.num_agents, 1)

        return self.state.clone(), self.reward, self.final_state, self.observation_space

    def step(self):


    def generate_observation(self, ode_int_output, parameter_matrix):
        if self.obs_type == "agent_only":
            return ode_int_output, ode_int_output

        elif self.obs_type == "all_shared" and not self.trade_actions:
            result = ode_int_output[:, 1:].flatten().repeat(self.num_agents, 1)
            result = torch.cat((torch.eye(self.num_agents), ode_int_output[:, 0].view(self.num_agents, 1), result), dim=1)  # 1 for each agent and then a overall, and yse for each agent
            return result, ode_int_output

        elif self.obs_type == "all_shared" and self.trade_actions:
            result = ode_int_output[:, 1:].flatten().repeat(self.num_agents, 1)
            trade_action_list = ((parameter_matrix[:, -1] != 1).float()).repeat(self.num_agents, 1)
            result = torch.cat((torch.eye(self.num_agents), ode_int_output[:, 0].view(self.num_agents, 1), result, trade_action_list), dim=1)  # 1 for each agent and then a overall, and yse for each agent then the trade action list
            return result, ode_int_output

    def _perform_step(self, action, next_t):

        parameter_matrix = self._get_parameters(action)

        parameter_vector = parameter_matrix.flatten()
        parameter_vector = torch.cat((parameter_vector, torch.tensor([self.num_agents])))

        ode_input = torch.cat((self.reward_space[:, 0:3], torch.zeros((self.num_agents, 1))), dim=1)

        traj_one_step = odeint(ays.AYS_rescaled_rhs_marl2, ode_input.flatten(), [self.t, next_t], args=tuple(parameter_vector.tolist()), mxstep=50000)

        return torch.tensor(traj_one_step[1]).view(-1, 4), parameter_matrix  # A Y S E output

    def get_reward_function(self, action=None):
        def reward_distance_PB(agent):
            if self._inside_planetary_boundaries(agent):
                self.reward[agent] = torch.norm(self.reward_space[agent, :3] - self.PB_2[agent])
            else:
                self.reward[agent] = 0.0

        def incentivised_reward_distance_PB(agent):
            if self._inside_A_pb(agent):
                new_reward = torch.norm(self.reward_space[agent, :3] - self.PB_4[agent])
            #     #  new_reward = torch.sqrt(4 * (torch.abs(self.reward_space[agent, 0] - self.PB_4[agent, 0])) ** 2 + (torch.abs(self.reward_space[agent, 1] - self.PB_4[agent, 1])) ** 2 + (torch.abs(self.reward_space[agent, 2] - self.PB_4[agent, 2])) ** 2)  # weighted by 4 to the a goal
            else:
                new_reward = 0.0

            if self.old_reward[agent] > new_reward:
                self.reward[agent] = -new_reward
            elif self.old_reward[agent] < new_reward:
                self.reward[agent] = new_reward
            else:
                self.reward[agent] = 0.0

            self.old_reward[agent] = new_reward

        def reward_distance_Y(agent):
            self.reward[agent] = torch.abs(self.reward_space[agent, 1] - self.PB_3[agent, 1])  # max y

        def reward_distance_E(agent):
            self.reward[agent] = torch.abs(self.reward_space[agent, 3] - self.PB_3[agent, 3])  # max e

        def reward_distance_A(agent):
            self.reward[agent] = torch.abs(self.reward_space[agent, 0] - self.PB_3[agent, 0]) / 4  # max a

        for agent in range(self.num_agents):
            if self.reward_type[agent] == 'PB':
                reward_distance_PB(agent)
            elif self.reward_type[agent] == 'IPB':
                incentivised_reward_distance_PB(agent)
            elif self.reward_type[agent] == 'max_Y':
                reward_distance_Y(agent)
            elif self.reward_type[agent] == 'max_E':
                reward_distance_E(agent)
            elif self.reward_type[agent] == 'max_A':
                reward_distance_A(agent)
            elif self.reward_type[agent] is None:
                print("ERROR! You have to choose a reward function!\n",
                      "Available Reward functions for this environment are: PB, rel_share, survive, desirable_region!")
                exit(1)
            else:
                print("ERROR! The reward function you chose is not available! " + self.reward_type[agent])
                self.print_debug_info()
                sys.exit(1)

    def calculate_expected_final_reward(self, agent):
        """
        Get the reward in the last state, expecting from now on always default.
        This is important since we break up simulation at final state, but we do not want the agent to
        find trajectories that are close (!) to final state and stay there, since this would
        result in a higher total reward.
        """
        remaining_steps = self.max_steps - self.t
        discounted_future_reward = 0.
        for i in range(remaining_steps):
            discounted_future_reward += self.gamma ** i * self.reward[agent]

        return discounted_future_reward

    @staticmethod
    def _compactification(x, x_mid):
        if x == 0:
            return 0.
        if x == np.infty:
            return 1.
        return x / (x + x_mid)

    @staticmethod
    def _inv_compactification(y, x_mid):
        if y == 0:
            return 0.
        if np.allclose(y, 1):
            return np.infty
        return x_mid * y / (1 - y)

    def _inside_A_pb(self, agent):
        e = self.state[agent, 0]
        y = self.state[agent, 1]
        a = self.state[agent, 2]
        is_inside = True

        if a > self.A_PB[agent]:
            is_inside = False
            # print("Outside PB!")
        return is_inside

    def _inside_planetary_boundaries(self, agent):
        e = self.state[agent, 0]
        y = self.state[agent, 1]
        a = self.state[agent, 2]
        is_inside = True

        if a > self.A_PB[agent] or y < self.Y_SF[agent] or e > self.E_LIMIT[agent]:
            is_inside = False
            # print("Outside PB!")
        return is_inside

    def _inside_planetary_boundaries_all(self):
        e = self.state[:, 0]
        y = self.state[:, 1]
        a = self.state[:, 2]
        is_inside = True

        if torch.all(a > self.A_PB) or torch.all(y < self.Y_SF) or torch.all(e > self.E_LIMIT):
            is_inside = False
            # print("Outside PB!")
        return is_inside

    def _arrived_at_final_state(self, agent):
        e = self.state[agent, 0]
        y = self.state[agent, 1]
        a = self.state[agent, 2]

        if torch.abs(e - self.green_fp[agent, 0]) < self.final_radius[agent] \
                and torch.abs(y - self.green_fp[agent, 1]) < self.final_radius[agent]\
                and torch.abs(a - self.green_fp[agent, 2]) < self.final_radius[agent]:
            return True
        elif torch.abs(e - self.brown_fp[agent, 0]) < self.final_radius[agent]\
                and torch.abs(y - self.brown_fp[agent, 1]) < self.final_radius[agent]\
                and torch.abs(a - self.brown_fp[agent, 2]) < self.final_radius[agent]:
            return True
        else:
            return False

    def green_fixed_point(self, agent):
        e = self.state[agent, 0]
        y = self.state[agent, 1]
        a = self.state[agent, 2]

        if torch.abs(e - self.green_fp[agent, 0]) < self.final_radius[agent] \
                and torch.abs(y - self.green_fp[agent, 1]) < self.final_radius[agent]\
                and torch.abs(a - self.green_fp[agent, 2]) < self.final_radius[agent]:
            return True
        else:
            return False

    def _good_final_state(self, agent):
        e = self.state[agent, 0]
        y = self.state[agent, 1]
        a = self.state[agent, 2]
        if np.abs(e - self.green_fp[agent, 0]) < self.final_radius[agent]\
                and np.abs(y - self.green_fp[agent, 1]) < self.final_radius[agent]\
                and np.abs(a - self.green_fp[agent, 2]) < self.final_radius[agent]:
            return True
        else:
            return False

    def which_final_state(self, agent):
        e = self.state[agent, 0]
        y = self.state[agent, 1]
        a = self.state[agent, 2]
        if np.abs(a - self.green_fp[agent, 0]) < self.final_radius[agent] and np.abs(y - self.green_fp[agent, 1]) < self.final_radius[agent] and np.abs(a - self.green_fp[agent, 2]) < self.final_radius[agent]:
            # print("ARRIVED AT GREEN FINAL STATE WITHOUT VIOLATING PB!")
            return Basins.GREEN_FP
        elif np.abs(a - self.brown_fp[agent,  0]) < self.final_radius[agent] and np.abs(y - self.brown_fp[agent, 1]) < self.final_radius[agent] and np.abs(a - self.brown_fp[agent, 2]) < self.final_radius[agent]:
            return Basins.BLACK_FP
        else:
            # return Basins.OUT_PB
            return self._which_PB(agent)

    def _which_PB(self, agent):
        """ To check which PB has been violated"""
        if self.state[agent, 2] >= self.A_PB[agent]:
            return Basins.A_PB
        elif self.state[agent, 1] <= self.Y_SF[agent]:
            return Basins.Y_SF
        elif self.state[agent, 0] <= 0:
            return Basins.E_PB
        else:
            return Basins.OUT_OF_TIME

    def random_StartPoint(self):

        self.state = torch.tensor([0, 0, 0]).repeat(self.num_agents, 1)
        while not self._inside_planetary_boundaries_all():
            self.state = torch.tensor(np.random.uniform(size=(self.current_state.size(0), 2)))

        return self.state

    def current_state_region_StartPoint(self):
        self.state = torch.tensor([0, 0, 0]).repeat(self.num_agents, 1)
        limit_start = 0.05

        while not self._inside_planetary_boundaries_all():

            adjustment = torch.tensor(
                np.random.uniform(low=-limit_start, high=limit_start, size=(self.current_state.size(0), 2)))
            self.state = self.current_state.clone()
            self.state[:, :2] += adjustment

            const_val = self.state[0, 0]
            self.state[:, 0] = const_val

            # homogeneous (comment out below if want heterogeneous)
            if self.homogeneous:
                all_equal = (self.state == self.state[0]).all()
                if not all_equal:
                    self.state[:] = self.state[0]

            assert torch.allclose(self.state[:, 0], const_val), "First column values are not equal."

        return self.state

    def _inside_box(self):
        """
        This function is needed to check whether our system leaves the predefined box (1,1,1).
        If values turn out to be negative, this is physically false, and we stop simulation and treat as a final state.
        """
        inside_box = True
        for x in self.state:
            if x < 0:
                x = 0
                inside_box = False
        return inside_box

    def _get_parameters(self, action=None):

        """
        This function is needed to return the parameter set for the chosen management option.
        Here the action numbers are really transformed to parameter lists, according to the chosen
        management option.
        Parameters:
            -action_number: Number of the action in the actionset.
             Can be transformed into: 'default', 'degrowth' ,'energy-transformation' or both DG and ET at the same time
        """
        if action is None:
            action = torch.tensor(0).repeat(self.num_agents, 1)
        #
        # print(action)
        # print(type(action))
        # print(action[0])
        # print(type(action[0]))
        # sys.exit()

        selected_rows = self.action_space[action.squeeze(), :]
        if self.trade_actions:
            action_matrix = selected_rows.view(self.num_agents, 3)
        else:
            action_matrix = selected_rows.view(self.num_agents, 2)

        mask_1 = action_matrix[:, 0].unsqueeze(1)
        mask_2 = action_matrix[:, 1].unsqueeze(1)
        if self.trade_actions:
            mask_3 = action_matrix[:, 2].unsqueeze(1)
        else:
            mask_3 = torch.tensor([False]).repeat(self.num_agents, 1)

        if torch.all(mask_3):
            pass
        elif torch.any(mask_3):
            mask_3 = ~mask_3
        else:
            pass

        beta = torch.where(mask_1, self.beta_LG, self.beta)
        sigma = torch.where(mask_2, self.sigma_ET, self.sigma)
        trade = torch.where(mask_3, self.trade_inflicted, self.trade)  # only works with 2 agents atm need to change that later on

        parameter_matrix = torch.cat((beta, self.eps, self.phi, self.rho, sigma, self.tau_A, self.tau_S, self.theta, trade), dim=1)

        return parameter_matrix

    def get_plot_state_list(self):
        return self.state, self.reward_space




