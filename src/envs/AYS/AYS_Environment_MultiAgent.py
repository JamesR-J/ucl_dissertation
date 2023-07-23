"""
This is the implementation of the AYS Environment in the form
that it can used within the Agent-Environment interface 
in combination with the DRL-agent.

@author: Felix Strnad, Theodore Wolf

"""

import sys
import torch

import numpy as np
from scipy.integrate import odeint
from . import ays_model as ays
# import ays_model as ays

# from . import ays_general
from .Basins import Basins
# from Basins import Basins
from gym import Env

import mpl_toolkits.mplot3d as plt3d
import matplotlib.pyplot as plt

from matplotlib.font_manager import FontProperties
from matplotlib.offsetbox import AnchoredText
from .AYS_3D_figures import create_figure
from . import AYS_3D_figures as ays_plot
import os

SMALL_SIZE = 12
MEDIUM_SIZE = 14
BIGGER_SIZE = 16
plt.rc('font', size=SMALL_SIZE)  # controls default text sizes
plt.rc('axes', titlesize=MEDIUM_SIZE)  # fontsize of the axes title
plt.rc('axes', labelsize=MEDIUM_SIZE)  # fontsize of the x and y labels
plt.rc('xtick', labelsize=SMALL_SIZE)  # fontsize of the tick labels
plt.rc('ytick', labelsize=SMALL_SIZE)  # fontsize of the tick labels
plt.rc('legend', fontsize=MEDIUM_SIZE)  # legend fontsize
plt.rc('figure', titlesize=BIGGER_SIZE)  # fontsize of the figure title


@np.vectorize
def inv_compactification(y, x_mid):
    if y == 0:
        return 0.
    if np.allclose(y, 1):
        return np.infty
    return x_mid * y / (1 - y)


from inspect import currentframe, getframeinfo


def get_linenumber():
    print_debug_info()
    print("Line: ")
    cf = currentframe()
    return cf.f_back.f_lineno


def print_debug_info():
    frameinfo = getframeinfo(currentframe())
    print("File: ", frameinfo.filename)


class AYS_Environment(Env):
    """
    The environment is based on Kittel et al. 2017, and contains in part code adapted from 
    https://github.com/timkittel/ays-model/ . 
    This Environment describes the 3D implementation of a simple model for the development of climate change, wealth
    and energy transformation which is inspired by the model from Kellie-Smith and Cox.
    Dynamic variables are :
        - excess atmospheric carbon stock A
        - the economic output/production Y  (similar to wealth)
        - the renewable energy knowledge stock S
    
    Parameters
    ----------
         - sim_time: Timestep that will be integrated in this simulation step
          In each grid point the agent can choose between subsidy None, A, B or A and B in combination. 
    """
    dimensions = np.array(['A', 'Y', 'S'])
    management_options = ['default', 'LG', 'ET', 'LG+ET']
    action_space = torch.tensor([[False, False], [True, False], [False, True], [True, True]])
    action_space_number = np.arange(len(action_space))
    # AYS example from Kittel et al. 2017:
    tau_A = 50  # carbon decay - single val
    tau_S = 50  # renewable knowledge stock decay - probs single val
    beta = 0.03  # economic output growth - multi val
    beta_LG = 0.015  # halved economic output growth - multi val
    eps = 147  # energy efficiency param - single val
    A_offset = 600  # i have no idea # TODO check this
    theta = beta / (950 - A_offset)  # beta / ( 950 - A_offset(=350) )
    # theta = 8.57e-5

    rho = 2.  # renewable knowledge learning rate - single val?
    sigma = 4e12  # break even knowledge - multi val
    sigma_ET = sigma * 0.5 ** (1 / rho)  # can't remember the change but it's somewhere - multi val
    # sigma_ET = 2.83e12

    phi = 4.7e10

    AYS0 = [240, 7e13, 5e11]

    possible_test_cases = [[0.4949063922255394, 0.4859623171738628, 0.5], [0.42610779, 0.52056811, 0.5]]

    def __init__(self, discount=0.99, t0=0, dt=1, reward_type='PB', max_steps=600, image_dir='./images/', run_number=0,
                 plot_progress=False, num_agents=2, **kwargs):
        self.management_cost = 0.5
        self.image_dir = image_dir
        self.run_number = run_number
        self.plot_progress = plot_progress
        self.max_steps = max_steps
        self.gamma = discount

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

        # self.reward_type = reward_type
        self.reward_type = [reward_type] * self.num_agents
        # self.reward_function = self.get_reward_function(reward_type)

        timeStart = 0
        intSteps = 10  # integration Steps
        self.t = self.t0 = t0
        self.dt = dt

        self.sim_time_step = np.linspace(timeStart, dt, intSteps)

        self.green_fp = torch.tensor([0, 1, 1]).repeat(self.num_agents, 1)
        self.brown_fp = torch.tensor([0.6, 0.4, 0]).repeat(self.num_agents, 1)
        self.final_radius = torch.tensor([0.05]).repeat(self.num_agents, 1)  # Attention depending on how large the radius is, the BROWN_FP can be reached!
        self.color_list = ays_plot.color_list

        self.X_MID = [240, 7e13, 5e11]

        # Definitions from outside
        self.current_state = torch.tensor([0.5, 0.5, 0.5]).repeat(self.num_agents, 1)
        self.state = self.start_state = self.current_state
        self.observation_space = self.state
        self.emissions = torch.tensor([0.0]).repeat(self.num_agents, 1)

        """
        This values define the planetary boundaries of the AYS model
        """
        self.A_PB = torch.tensor([self._compactification(ays.boundary_parameters["A_PB"], self.X_MID[0])]).repeat(self.num_agents, 1)  # Planetary boundary: 0.5897
        self.Y_SF = torch.tensor([self._compactification(ays.boundary_parameters["W_SF"], self.X_MID[1])]).repeat(self.num_agents, 1)  # Social foundations as boundary: 0.3636  # TODO almost keep a global AYS with boundaries but then also individual agent ones
        self.S_LIMIT = torch.tensor([0.0]).repeat(self.num_agents, 1)
        self.PB = torch.cat((self.A_PB, self.Y_SF, self.S_LIMIT), dim=1)

        # print("Init AYS Environment!",
        #       "\nReward Type: " + reward_type,
        #       "\nSustainability Boundaries [A_PB, Y_SF, S_ren]: ", inv_compactification(self.PB, self.X_MID))

    def step(self, action: int):
        """
        This function performs one simulation step in an RL algorithm.
        It updates the state and returns a reward according to the chosen reward-function.
        """

        next_t = self.t + self.dt

        result = self._perform_step(action, next_t)
        self.state = result[:, 0:3]
        self.emissions = result[:, 3]

        if not self.final_state.bool().any():
            assert torch.all(self.state[:, 0] == self.state[0, 0]), "Values in the first column are not all equal"

        self.t = next_t

        self.get_reward_function()  # TODO check if this might be needed before step is done to evaluate the current state, not the next state!

        for agent in range(self.num_agents):
            if self._arrived_at_final_state(agent):
                self.final_state[agent] = True
            # if not self._inside_planetary_boundaries(agent):
            #     self.final_state[agent] = True
            if self.final_state[agent] and (self.reward_type == "PB" or self.reward_type == "policy_cost"):  # TODO shold this include the new PB_ext or PB_new
                self.reward[agent] += self.calculate_expected_final_reward(agent)
            if self.final_state[agent] and self.reward_type == "PB_ext":  # TODO means can get the final step if done ie the extra or less reward for PB_ext - bit of a dodgy workaround may look at altering the reward placement in the step function
                self.get_reward_function()

        # print(self.reward)

        return self.state, self.reward, self.final_state, None

    def _perform_step(self, action, next_t):

        parameter_matrix = self._get_parameters(action)

        parameter_vector = parameter_matrix.flatten()
        parameter_vector = torch.cat((parameter_vector, torch.tensor([self.num_agents])))

        ode_input = torch.cat((self.state, torch.zeros((self.num_agents, 1))), dim=1)

        traj_one_step = odeint(ays.AYS_rescaled_rhs_marl2, ode_input.flatten(), [self.t, next_t], args=tuple(parameter_vector.tolist()), mxstep=50000)

        return torch.tensor(traj_one_step[1]).view(-1, 4)

    def reset(self):
        # self.state=np.array(self.random_StartPoint())
        self.state = self.current_state_region_StartPoint()
        # self.state=np.array(self.current_state)
        self.final_state = torch.tensor([False]).repeat(self.num_agents, 1)
        self.t = self.t0
        return self.state

    def reset_for_state(self, state=None):  # TODO check this
        if state is None:
            self.start_state = self.state = self.current_state
        else:
            self.start_state = self.state = state
        self.final_state = torch.tensor([False]).repeat(self.num_agents, 1)
        self.t = self.t0
        return self.state

    def get_reward_function(self):
        def reward_final_state(agent, action=0):
            """
            Reward in the final  green fixpoint_good 1., else 0.
            """
            if self._good_final_state(agent):
                self.reward[agent] = 1.0
            else:
                self.reward[agent] = -0.00000000000001

        def reward_rel_share(agent, action=0):
            """
            We want to:
            - maximize the knowledge stock of renewables S 
            - minimize the excess atmospheric carbon stock A 
            - maximize the economic output Y!
            """
            a, y, s = self.state[agent]
            if self._inside_planetary_boundaries(agent):
                self.reward[agent] = 1.0
            else:
                self.reward[agent] = 0.0

            self.reward[agent] *= s

        def reward_desirable_region(agent, action=0):
            a, y, s = self.state[agent]
            desirable_share_renewable = 0.3
            self.reward[agent] = 0.0
            if s >= desirable_share_renewable:
                self.reward[agent] = 1.

        def reward_survive(agent, action=0):
            if self._inside_planetary_boundaries(agent):
                self.reward[agent] = 1.0
            else:
                self.reward[agent] = -0.0000000000000001

        def reward_survive_cost(agent, action=0):
            cost_managment = 0.03
            if self._inside_planetary_boundaries(agent):
                self.reward[agent] = 1.0
                if self.management_options[action] != 'default':
                    self.reward[agent] -= cost_managment

            else:
                self.reward[agent] = -0.0000000000000001

        def policy_cost(agent, action=0):  # TODO check this if works
            """@Theo Wolf, we add a cost to using management options """
            if self._inside_planetary_boundaries(agent):
                self.reward[agent] = torch.norm(self.state[agent] - self.PB[agent])
            else:
                self.reward[agent] = 0.0  # TODO make a version with the -100 for hitting planetary boundary
            if self.management_options[action] != 'default':
                #reward -= self.management_cost  # cost of using management
                self.reward[agent] *= self.management_cost
                if self.management_options[action] == 'LG+ET':
                    # reward -= self.management_cost  # we add more cost for using both actions
                    self.reward[agent] *= self.management_cost

        def simple(agent, action=0):
            """@Theo Wolf, much simpler scheme that aims for what we actually want: go green"""
            if self._inside_planetary_boundaries(agent):
                self.reward[agent] = 0
            else:
                self.reward[agent] = -1

        def reward_distance_PB(agent, action=0):
            self.reward[agent] = 0.0

            if self._inside_planetary_boundaries(agent):
                self.reward[agent] = torch.norm(self.state[agent]-self.PB[agent])
            else:
                self.reward[agent] = 0.0

        def reward_distance_PB_new(agent, action=0):
            self.reward[agent] = 0.0

            if self._inside_planetary_boundaries(agent):
                self.reward[agent] = torch.norm(self.state[agent]-self.PB[agent])
            else:
                # print(self._which_PB(agent))
                if self.state[agent, 0] >= self.A_PB[agent]:   # TODO implemented bad reward for hitting PB is that good idea idk
                    self.reward[agent] += -100
                if self.state[agent, 1] <= self.Y_SF[agent]:
                    self.reward[agent] += -100

        def reward_distance_PB_extended(agent, action=0):  # TODO check this reward func
            self.reward[agent] = 0.0

            if self._inside_planetary_boundaries(agent):
                self.reward[agent] = torch.norm(self.state[agent] - self.PB[agent])
            else:
                self.reward[agent] = 0.0

            if self.final_state[agent]:
                if self._good_final_state(agent):
                    if self.which_final_state(agent) == 2:  # green_fp  # TODO change this to the basins.a_pb thingo
                        self.reward[agent] = 1000
                    elif self.which_final_state(agent) == 1:  # brown/black_fp
                        self.reward[agent] = -1000


        for agent in range(self.num_agents):
            if self.reward_type[agent] == 'final_state':
                reward_final_state(agent)
            elif self.reward_type[agent] == 'ren_knowledge':
                reward_rel_share(agent)
            elif self.reward_type[agent] == 'desirable_region':
                reward_desirable_region(agent)
            elif self.reward_type[agent] == 'PB':
                reward_distance_PB(agent)
            elif self.reward_type[agent] == 'PB_new':
                reward_distance_PB_new(agent)
            elif self.reward_type[agent] == 'PB_ext':
                reward_distance_PB_extended(agent)
            elif self.reward_type[agent] == 'survive':
                reward_survive(agent)
            elif self.reward_type[agent] == 'survive_cost':
                reward_survive_cost(agent)
            elif self.reward_type[agent] == "policy_cost":
                policy_cost(agent)
            elif self.reward_type[agent] == "simple":
                simple(agent)
            elif self.reward_type[agent] == None:
                print("ERROR! You have to choose a reward function!\n",
                      "Available Reward functions for this environment are: PB, rel_share, survive, desirable_region!")
                exit(1)
            else:
                print("ERROR! The reward function you chose is not available! " + self.reward_type[agent])
                print_debug_info()
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

    def _compactification(self, x, x_mid):
        if x == 0:
            return 0.
        if x == np.infty:
            return 1.
        return x / (x + x_mid)

    def _inv_compactification(self, y, x_mid):
        if y == 0:
            return 0.
        if np.allclose(y, 1):
            return np.infty
        return x_mid * y / (1 - y)

    def _inside_planetary_boundaries(self, agent):  # TODO confirm this is correct
        a = self.state[agent, 0]
        y = self.state[agent, 1]
        s = self.state[agent, 2]
        is_inside = True

        if a > self.A_PB[agent] or y < self.Y_SF[agent] or s < self.S_LIMIT[agent]:
            is_inside = False
            # print("Outside PB!")
        return is_inside

    def _inside_planetary_boundaries_all(self):  # TODO confirm this is correct
        a = self.state[:, 0]
        y = self.state[:, 1]
        s = self.state[:, 2]
        is_inside = True

        if torch.all(a > self.A_PB) or torch.all(y < self.Y_SF) or torch.all(s < self.S_LIMIT):
            is_inside = False
            # print("Outside PB!")
        return is_inside

    def _arrived_at_final_state(self, agent):  # TODO confirm this is correct
        a = self.state[agent, 0]
        y = self.state[agent, 1]
        s = self.state[agent, 2]

        if torch.abs(a - self.green_fp[agent, 0]) < self.final_radius[agent] \
                and torch.abs(y - self.green_fp[agent, 1]) < self.final_radius[agent]\
                and torch.abs(s - self.green_fp[agent, 2]) < self.final_radius[agent]:
            return True
        elif torch.abs(a - self.brown_fp[agent, 0]) < self.final_radius[agent]\
                and torch.abs(y - self.brown_fp[agent, 1]) < self.final_radius[agent]\
                and torch.abs(s - self.brown_fp[agent, 2]) < self.final_radius[agent]:
            return True
        else:
            return False

    def _good_final_state(self, agent):  # TODO confirm this is correct
        a = self.state[agent, 0]
        y = self.state[agent, 1]
        s = self.state[agent, 2]
        if np.abs(a - self.green_fp[agent, 0]) < self.final_radius[agent]\
                and np.abs(y - self.green_fp[agent, 1]) < self.final_radius[agent]\
                and np.abs(s - self.green_fp[agent, 2]) < self.final_radius[agent]:
            return True
        else:
            return False

    def which_final_state(self, agent):  # TODO confirm this is correct
        a = self.state[agent, 0]
        y = self.state[agent, 1]
        s = self.state[agent, 2]
        if np.abs(a - self.green_fp[agent, 0]) < self.final_radius[agent] and np.abs(y - self.green_fp[agent, 1]) < self.final_radius[agent] and np.abs(s - self.green_fp[agent, 2]) < self.final_radius[agent]:
            # print("ARRIVED AT GREEN FINAL STATE WITHOUT VIOLATING PB!")
            return Basins.GREEN_FP
        elif np.abs(a - self.brown_fp[agent,  0]) < self.final_radius[agent] and np.abs(y - self.brown_fp[agent, 1]) < self.final_radius[agent] and np.abs(s - self.brown_fp[agent, 2]) < self.final_radius[agent]:
            return Basins.BLACK_FP
        else:
            # return Basins.OUT_PB
            return self._which_PB(agent)

    def _which_PB(self, agent):  # TODO confirm this is correct
        """ To check which PB has been violated"""
        if self.state[agent, 0] >= self.A_PB[agent]:
            return Basins.A_PB
        elif self.state[agent, 1] <= self.Y_SF[agent]:
            return Basins.Y_SF
        elif self.state[agent, 2] <= 0:
            return Basins.S_PB
        else:
            return Basins.OUT_OF_TIME

    def get_plot_state_list(self):
        return self.state.tolist()[:3]

    def prepare_action_set(self, state):
        return np.arange(len(self.action_space) - 1)

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

            # print(self.state)

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
        # if action < len(self.action_space):
        #     action_tuple = self.action_space[action]
        # else:
        #     print("ERROR! Management option is not available!" + str(action))
        #     print(get_linenumber())
        #     sys.exit(1)

        if action is None:
            action = torch.tensor([0]).repeat(self.num_agents, 1)

        # if type(action) == int:
        #     action = torch.tensor([action]).view(self.num_agents, 1)

        selected_rows = self.action_space[action.squeeze(), :]
        action_matrix = selected_rows.view(self.num_agents, 2)

        mask_1 = action_matrix[:, 0].unsqueeze(1)
        mask_2 = action_matrix[:, 1].unsqueeze(1)

        beta = torch.where(mask_1, self.beta_LG, self.beta)
        sigma = torch.where(mask_2, self.sigma_ET, self.sigma)

        parameter_matrix = torch.cat((beta, self.eps, self.phi, self.rho, sigma, self.tau_A, self.tau_S, self.theta), dim=1)

        return parameter_matrix

    def plot_run(self, learning_progress, fig, axes, colour, fname=None,):
        timeStart = 0
        intSteps = 2  # integration Steps
        dt = 1
        sim_time_step = np.linspace(timeStart, self.dt, intSteps)
        if axes is None:
            fig, ax3d = create_figure()
        else:
            ax3d = axes
        start_state = learning_progress[0][0]

        for state_action in learning_progress:
            state = state_action[0]
            action = state_action[1]
            parameter_list = self._get_parameters(action)
            traj_one_step = odeint(ays.AYS_rescaled_rhs, state, sim_time_step, args=parameter_list[0])
            # Plot trajectory
            my_color = ays_plot.color_list[action]
            ax3d.plot3D(xs=traj_one_step[:, 0], ys=traj_one_step[:, 1], zs=traj_one_step[:, 2],
                        color=colour, alpha=0.3, lw=3)

        # Plot from startpoint only one management option to see if green fix point is easy to reach:
        # self.plot_current_state_trajectories(ax3d)
        ays_plot.plot_hairy_lines(20, ax3d)

        final_state = self.which_final_state().name
        if fname is not None:
            plt.savefig(fname)
        #plt.show()

        return fig, ax3d

    def observed_states(self):
        return self.dimensions

    def plot_current_state_trajectories(self, ax3d):
        # Trajectories for the current state with all possible management options
        time = np.linspace(0, 300, 1000)

        for action_number in range(len(self.action_space)):
            parameter_list = self._get_parameters(action_number)
            my_color = self.color_list[action_number]
            traj_one_step = odeint(ays.AYS_rescaled_rhs, self.current_state, time, args=parameter_list[0])
            ax3d.plot3D(xs=traj_one_step[:, 0], ys=traj_one_step[:, 1], zs=traj_one_step[:, 2],
                        color=my_color, alpha=.7, label=None)

    def save_traj_final_state(self, learners_path, file_path, episode):
        final_state = self.which_final_state().name

        states = np.array(learners_path)[:, 0]
        start_state = states[0]
        a_states = list(zip(*states))[0]
        y_states = list(zip(*states))[1]
        s_states = list(zip(*states))[2]

        actions = np.array(learners_path)[:, 1]
        rewards = np.array(learners_path)[:, 2]

        full_file_path = file_path + '/DQN_Path/' + final_state + '/'
        if not os.path.isdir(full_file_path):
            os.makedirs(full_file_path)

        text_path = (full_file_path +
                     str(self.run_number) + '_' + 'path_' + str(start_state) + '_episode' + str(episode) + '.txt')
        with open(text_path, 'w') as f:
            f.write("# A  Y  S   Action   Reward \n")
            for i in range(len(learners_path)):
                f.write("%s  %s  %s   %s   %s \n" % (a_states[i], y_states[i], s_states[i], actions[i], rewards[i]))
        f.close()
        print('Saved :' + text_path)

    def save_traj(self, ax3d, fn):
        ax3d.legend(loc='best', prop={'size': 12})
        plt.savefig(fname=fn)
        plt.close()

    def define_test_points(self):
        testpoints = [
            [0.49711988, 0.49849855, 0.5],
            [0.48654806, 0.51625583, 0.5],
            [0.48158348, 0.50938806, 0.5],
            [0.51743486, 0.45828958, 0.5],
            [0.52277734, 0.49468274, 0.5],
            [0.49387675, 0.48199759, 0.5],
            [0.45762969, 0.50656114, 0.5]
        ]
        return testpoints

    def test_Q_states(self):
        # The Q values are choosen here in the region of the knick and the corner 
        testpoints = [
            [0.5, 0.5, 0.5],
            [0.48158348, 0.50938806, 0.5],  # points around current state
            [0.51743486, 0.45828958, 0.5],
            [0.52277734, 0.49468274, 0.5],
            [0.49711988, 0.49849855, 0.5],
            [0.5642881652513302, 0.4475774101441196, 0.5494879542441825],  # From here on for knick to green FP
            [0.5677565382994565, 0.4388184256945361, 0.5553589418072845],
            [0.5642881652513302, 0.4475774101441196, 0.5494879542441825],
            [0.5667064632786063, 0.4417642808582638, 0.5534355600174762],
            [0.5677565382994565, 0.4388184256945361, 0.5553589418072845],
            [0.5667064632786063, 0.4417642808582638, 0.5534355600174762],
            [0.5642881652513302, 0.4475774101441196, 0.5494879542441825],
            [0.5667064632786063, 0.4417642808582638, 0.5534355600174762],
            [0.5677565382994565, 0.4388184256945361, 0.5553589418072845],
            [0.5667064632786063, 0.4417642808582638, 0.5534355600174762],
            [0.565551647191721, 0.4446849282686741, 0.5514780427327116],
            [0.5667064632786063, 0.4417642808582638, 0.5534355600174762],
            [0.5732889740892303, 0.40670386098365746, 0.5555233190964499],
            [0.575824650184652, 0.4053645419804867, 0.4723020776953208],
            [0.5770448313058577, 0.4048031241155815, 0.418890921031026],  # From here on for knick to black FP
            [0.5731695199856403, 0.40703303828389187, 0.5611291038925613],
            [0.5742215704891825, 0.42075928220225944, 0.4638131691273601],
            [0.5763299679962532, 0.411095026888074, 0.4294020150808698],
            [0.5722546035810613, 0.41315124675768045, 0.5695919593600399],
            [0.5762062083990029, 0.405168276738863, 0.4567816125395152],
            [0.5762327254875753, 0.4052313013623205, 0.4568789522146076],
            [0.5770448313058577, 0.4048031241155815, 0.418890921031026],
            [0.5770448313058577, 0.4048031241155815, 0.418890921031026],
            [0.5726685871808355, 0.40709323935138103, 0.5727121746516005],
            [0.2841645298525685, 0.5742868996790442, 0.9699317116062534],  # From here on region of the shelter
            [0.32909951420599637, 0.6082136751752725, 0.9751810127843358],
            [0.5649255262907135, 0.4238116683903446, 0.8009508342049909],
            [0.04143141196994614, 0.9467759116676885, 0.9972458138530155],
        ]
        return testpoints

    def test_reward_functions(self):
        print(self.reward_type)
        print(self.state)
        self.state[0, 0] = 0.5899
        self.state[0, 1] = 0.362
        print(self.state)
        print(self.reward)
        self.get_reward_function()
        print(self.reward)
        sys.exit()










