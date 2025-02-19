# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Tests for compiler_opt.rl.env."""

import io
import contextlib
import ctypes
from unittest import mock
import subprocess
import os
import tempfile
from absl.testing import flagsaver

from typing import Dict, List, Optional

import tensorflow as tf
import numpy as np

from compiler_opt.rl import env
from compiler_opt.rl import corpus
from compiler_opt.rl import log_reader_test

_CLANG_PATH = '/test/clang/path'

_MOCK_MODULE = corpus.LoadedModuleSpec(
    name='module',
    loaded_ir=b'asdf',
    orig_options=('--opt_a', 'a', '--opt_b', 'b'),
)

_NUM_STEPS = 10


class MockTask(env.MLGOTask):
  """Implementation of mock task for testing."""

  def get_cmdline(self, clang_path: str, base_args: List[str],
                  interactive_base_path: Optional[str],
                  working_dir: str) -> List[str]:
    if interactive_base_path:
      interactive_args = [
          f'--interactive={interactive_base_path}',
      ]
    else:
      interactive_args = []
    return [clang_path] + base_args + interactive_args

  def get_module_scores(self, working_dir: str) -> Dict[str, float]:
    return {'default': 47}


# This mocks subprocess.Popen for interactive clang sessions
@contextlib.contextmanager
def mock_interactive_clang(cmdline, stderr, stdout):
  del stderr
  del stdout
  # do basic argument parsing
  fname = None
  for arg in cmdline:
    if arg.startswith('--interactive='):
      fname = arg[len('--interactive='):]
      break

  class MockProcess:

    def wait(self, timeout):
      pass

    def kill(self):
      pass

  if not fname:
    yield MockProcess()
    return
  # Create the fds for the pipes
  # (the env doesn't create the files, it assumes they are opened by clang)
  with io.FileIO(fname + '.out', 'wb+') as f_out:
    with io.FileIO(fname + '.in', 'rb+') as f_in:
      del f_in
      writer = log_reader_test.LogTestExampleBuilder(opened_file=f_out)
      # Write the header describing the features/rewards
      writer.write_header({
          'features': [{
              'name': 'times_called',
              'port': 0,
              'shape': [1],
              'type': 'int64_t',
          },],
          'score': {
              'name': 'reward',
              'port': 0,
              'shape': [1],
              'type': 'float',
          },
      })
      writer.write_newline()

      class MockInteractiveProcess(MockProcess):
        """Mock clang interactive process that writes the log."""

        def __init__(self):
          self._counter = 0

        # We poll the process at every call to get_observation to ensure the
        # clang process is still alive. So here, each time poll() is called,
        # write a new context
        def poll(self):
          if self._counter >= _NUM_STEPS:
            f_out.close()
            return None
          example_writer = log_reader_test.LogTestExampleBuilder(
              opened_file=f_out)
          example_writer.write_context_marker(f'context_{self._counter}')
          example_writer.write_observation_marker(0)
          example_writer.write_buff([self._counter], ctypes.c_int64)
          example_writer.write_newline()
          example_writer.write_outcome_marker(0)
          example_writer.write_buff([3.14], ctypes.c_float)
          example_writer.write_newline()
          self._counter += 1
          return None

      yield MockInteractiveProcess()


class ClangSessionTest(tf.test.TestCase):

  @mock.patch('subprocess.Popen')
  def test_clang_session(self, mock_popen):
    mock_task = MockTask()
    with env.clang_session(
        _CLANG_PATH, _MOCK_MODULE, MockTask,
        interactive=False) as clang_session:
      del clang_session
      cmdline = mock_task.get_cmdline(_CLANG_PATH,
                                      list(_MOCK_MODULE.orig_options), None,
                                      '/tmp/mock/tmp/file')
      mock_popen.assert_called_once_with(
          cmdline, stderr=subprocess.PIPE, stdout=subprocess.PIPE)

  @mock.patch('subprocess.Popen')
  def test_interactive_clang_session(self, mock_popen):
    mock_popen.side_effect = mock_interactive_clang

    with env.clang_session(
        _CLANG_PATH, _MOCK_MODULE, MockTask, interactive=True) as clang_session:
      for idx in range(_NUM_STEPS):
        obs = clang_session.get_observation()
        self.assertEqual(
            obs.obs['times_called'],
            np.array([idx], dtype=np.int64),
        )
        self.assertEqual(obs.context, f'context_{idx}')
      mock_popen.assert_called_once()

  @mock.patch('subprocess.Popen')
  def test_interactive_clang_temp_dir(self, mock_popen):
    mock_popen.side_effect = mock_interactive_clang
    working_dir = None

    with env.clang_session(
        _CLANG_PATH, _MOCK_MODULE, MockTask, interactive=True) as clang_session:
      for _ in range(_NUM_STEPS):
        obs = clang_session.get_observation()
        working_dir = obs.working_dir
        self.assertEqual(os.path.exists(working_dir), True)
    self.assertEqual(os.path.exists(working_dir), False)

    with tempfile.TemporaryDirectory() as td:
      with flagsaver.flagsaver(
          (env.compilation_runner._EXPLICIT_TEMPS_DIR, td)):  # pylint: disable=protected-access
        with env.clang_session(
            _CLANG_PATH, _MOCK_MODULE, MockTask,
            interactive=True) as clang_session:
          for _ in range(_NUM_STEPS):
            obs = clang_session.get_observation()
            working_dir = obs.working_dir
            self.assertEqual(os.path.exists(working_dir), True)
        self.assertEqual(os.path.exists(working_dir), True)


class MLGOEnvironmentTest(tf.test.TestCase):

  @mock.patch('subprocess.Popen')
  def test_env(self, mock_popen):
    mock_popen.side_effect = mock_interactive_clang

    test_env = env.MLGOEnvironmentBase(
        clang_path=_CLANG_PATH,
        task_type=MockTask,
        obs_spec={},
        action_spec={},
    )

    for env_itr in range(3):
      del env_itr
      step = test_env.reset(_MOCK_MODULE)
      self.assertEqual(step.step_type, env.StepType.FIRST)

      for step_itr in range(_NUM_STEPS - 1):
        del step_itr
        step = test_env.step(np.array([1], dtype=np.int64))
        self.assertEqual(step.step_type, env.StepType.MID)

      step = test_env.step(np.array([1], dtype=np.int64))
      self.assertEqual(step.step_type, env.StepType.LAST)
      self.assertNotEqual(test_env._iclang, test_env._clang)  # pylint: disable=protected-access

  @mock.patch('subprocess.Popen')
  def test_env_interactive_only(self, mock_popen):
    mock_popen.side_effect = mock_interactive_clang

    test_env = env.MLGOEnvironmentBase(
        clang_path=_CLANG_PATH,
        task_type=MockTask,
        obs_spec={},
        action_spec={},
        interactive_only=True,
    )

    for env_itr in range(3):
      del env_itr
      step = test_env.reset(_MOCK_MODULE)
      self.assertEqual(step.step_type, env.StepType.FIRST)

      for step_itr in range(_NUM_STEPS - 1):
        del step_itr
        step = test_env.step(np.array([1], dtype=np.int64))
        self.assertEqual(step.step_type, env.StepType.MID)

      step = test_env.step(np.array([1], dtype=np.int64))
      self.assertEqual(step.step_type, env.StepType.LAST)
      self.assertEqual(test_env._iclang, test_env._clang)  # pylint: disable=protected-access


if __name__ == '__main__':
  tf.test.main()
