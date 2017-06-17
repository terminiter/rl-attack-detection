import argparse
import gym
import os
import numpy as np
import tensorflow as tf
import cv2
from collections import deque

from gym.monitoring import VideoRecorder

import baselines.common.tf_util as U

from baselines import deepq
from baselines.common.misc_util import (
    boolean_flag,
    SimpleMonitor,
)
from baselines.common.atari_wrappers_deprecated import wrap_dqn
from baselines.deepq.experiments.atari.model import model, dueling_model
from baselines.deepq.li_attack import CarliniLi


def parse_args():
    parser = argparse.ArgumentParser("Run an already learned DQN model.")
    # Environment
    parser.add_argument("--env", type=str, required=True, help="name of the game")
    parser.add_argument("--model-dir", type=str, default=None, help="load model from this directory. ")
    parser.add_argument("--video", type=str, default=None, help="Path to mp4 file where the video of first episode will be recorded.")
    boolean_flag(parser, "stochastic", default=True, help="whether or not to use stochastic actions according to models eps value")
    boolean_flag(parser, "dueling", default=False, help="whether or not to use dueling model")
    parser.add_argument("--attack", type=str, default=None, help="Method to attack the model.")

    return parser.parse_args()


def make_env(game_name):
    env = gym.make(game_name + "NoFrameskip-v4")
    env = SimpleMonitor(env)
    env = wrap_dqn(env)
    return env


def test_gen(sess, obs, act, gt, mean, model, n_actions, step):
    obs = obs - mean[None]
    obs = obs * 1/255.0
    onehot_act = np.zeros((1, n_actions))
    onehot_act[0, act] = 1

    pred = model.predict(sess, obs, onehot_act)[0]

    pred = pred * 255.0
    pred = pred + mean[None]
    #print(gt[:, :, -1].shape, pred.shape)
    #print(np.sum(gt[:, :, -1][:, :, np.newaxis] - pred[0, :, :, :]))
    #cv2.imwrite('/home/yclin/viz/gt_{}.png'.format(step), gt[:, :, -1][:, :, np.newaxis])
    #cv2.imwrite('/home/yclin/viz/pred_{}.png'.format(step), pred[0, :, :, :])
    return pred[0, :, :, 0]


def play(env, acts, stochastic, video_path, attack=None, q_func=None):
    act = acts[0]
    adv_act = acts[1]

    num_episodes = 0
    step = 0
    video_recorder = None
    video_recorder = VideoRecorder(
        env, video_path, enabled=video_path is not None)
    obs = env.reset()
    pred_obs = deque(maxlen=4)
    sess = U.get_session()

    detection = 0.0
    attack_success = 0.0
    fp = 0.0

    if attack != None:
        from baselines.deepq.prediction.tfacvp.model import ActionConditionalVideoPredictionModel
        gen_dir = '/home/yclin/Workspace/rl-adversarial-attack-detection/baselines/baselines/deepq/prediction'
        model_path = os.path.join(gen_dir, 'models/PongNoFrameskip-v4-gray-model/train/model.ckpt-306490')
        mean_path = os.path.join(gen_dir, 'PongNoFrameskip-v4/mean.npy')

        mean = np.load(mean_path)
        with tf.variable_scope('G'):
            model = ActionConditionalVideoPredictionModel(num_act=env.action_space.n, num_channel=1, is_train=False)
            model.restore(sess, model_path, 'G')

    if attack == 'carliniL2':
        carliniLi = CarliniLi(sess, q_func, env.action_space.n)

    while True:
        #env.unwrapped.render()
        video_recorder.capture_frame()
        step += 1
        if attack == 'carliniL2':
            obs = (np.array(obs) - 127.5) / 255.0
            adv_obs = carliniLi.attack_single(obs, [0., 0., 0., 0., 1., 0.])
            print(np.min(adv_obs))
            adv_obs = np.array(adv_obs) * 255.0 + 127.5
            action = act(np.array(adv_obs), stochastic=stochastic)[0]
            obs, rew, done, info = env.step(action)
        else:
            # np.array(obs)[None]: (1, 84, 84, 4)
            adv_action = adv_act(np.array(obs)[None], stochastic=stochastic)[0]
            action = act(np.array(obs)[None], stochastic=stochastic)[0]
            #print(adv_action == action)

            # Defensive planning
            if step >= 4:
                pred_obs.append(test_gen(sess, old_obs, action, np.array(obs), mean, model,
                    env.action_space.n, step))
                if len(pred_obs) == 4:
                    pred_act = act(np.stack(pred_obs, axis=2)[None], stochastic=stochastic)[0]
                    #print("Step: {}".format(step))
                    #print(pred_act, action, pred_act == action)
                    if adv_action != action:
                        attack_success += 1
                        if pred_act != adv_action:
                            detection += 1
                    if pred_act != action:
                        fp += 1

        #print("Step: {}".format(step))
        #print("Action: {}".format(action))

            old_obs = np.array(obs)
            #obs, rew, done, info = env.step(adv_action)
            obs, rew, done, info = env.step(adv_action)
            """
            if step >= 4:
                if len(pred_obs) == 4:
                    obs, rew, done, info = env.step(pred_act)
            else:
                obs, rew, done, info = env.step(action)
            """
        if done:
            obs = env.reset()
            print("Attack success:", attack_success/step)
            print("Detection:", detection/attack_success)
            print("False Positive:", fp/step)

        if len(info["rewards"]) > num_episodes:
            if len(info["rewards"]) == 1 and video_recorder.enabled:
                # save video of first episode
                print("Saved video.")
                video_recorder.close()
                video_recorder.enabled = False
            print(info["rewards"][-1])
            num_episodes = len(info["rewards"])
            step = 0
            exit()



if __name__ == '__main__':
    with U.make_session(4) as sess:
        args = parse_args()
        env = make_env(args.env)
        q_func = dueling_model if args.dueling else model
        act, adv_act = deepq.build_act(
            make_obs_ph=lambda name: U.Uint8Input(env.observation_space.shape, name=name),
            q_func=q_func,
            num_actions=env.action_space.n,
            attack=args.attack)
        U.load_state(os.path.join(args.model_dir, "saved"))
        play(env, [act, adv_act], args.stochastic, args.video, args.attack, q_func=q_func)
