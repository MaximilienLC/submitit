# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
#

import os
import shlex
import signal
import subprocess
import sys
import time
import typing as tp
from pathlib import Path

from ..core import core, job_environment, logger, utils
from ..core.core import R

# pylint: disable-msg=too-many-arguments
# VALID_KEYS = {"timeout_min", "gpus_per_node", "tasks_per_node", "signal_delay_s", "visible_gpus", "setup"}

LOCAL_REQUEUE_RETURN_CODE = 144

# global variable storing unfinished processes of pickled jobs
# in case we need to reload them later
_PROCESSES: tp.Dict[str, "subprocess.Popen['bytes']"] = {}


class LocalJob(core.Job[R]):
    def __init__(
        self,
        folder: tp.Union[Path, str],
        job_id: str,
        tasks: tp.Sequence[int] = (0,),
        process: tp.Optional["subprocess.Popen['bytes']"] = None,
    ) -> None:
        super().__init__(folder, job_id, tasks)
        self._cancel_at_deletion = False
        # downcast sub-jobs to get proper typing
        self._sub_jobs: tp.Sequence["LocalJob[R]"] = self._sub_jobs
        # set process (to self and subjobs)
        self._process = process
        for sjob in self._sub_jobs:
            sjob._process = process

    def done(self, force_check: bool = False) -> bool:  # pylint: disable=unused-argument
        """Override to avoid using the watcher"""
        state = self.get_info()["jobState"]
        return state != "RUNNING"

    @property
    def state(self) -> str:
        """State of the job"""
        try:
            return self.get_info().get("jobState", "unknown")
        # I don't what is the exception returned and it's hard to reproduce
        except Exception:  # pylint: disable=broad-except
            return "UNKNOWN"

    def get_info(self, mode: str = "force") -> tp.Dict[str, str]:  # pylint: disable=unused-argument
        """Returns information about the job as a dict."""
        if self._process is None:
            state = "NO PROCESS AND NO RESULT"
            if self.paths.result_pickle.exists():
                state = "FINISHED"
            return {"jobState": state}
        poll = self._process.poll()
        if poll is None:
            state = "RUNNING"
        elif poll < 0:
            state = "INTERRUPTED"
        else:
            state = "FINISHED"
        return {"jobState": state}

    def cancel(self, check: bool = True) -> None:  # pylint: disable=unused-argument
        if self._process is not None:
            self._process.send_signal(signal.SIGINT)

    def _interrupt(self) -> None:
        """Sends preemption / timeout signal to the job (for testing purpose)"""
        if self._process is not None:
            self._process.send_signal(LocalJobEnvironment._usr_sig())

    def __del__(self) -> None:
        if self._cancel_at_deletion:
            if not self.get_info().get("jobState") == "FINISHED":
                self.cancel(check=False)
        # let's clear the process dict if we know it's finished
        if self.paths.result_pickle.exists():
            _PROCESSES.pop(self.job_id, None)

    # # # # # pickling below # # # # #

    def __getstate__(self) -> tp.Any:
        out = dict(self.__dict__)
        out["_process"] = None
        if self._process is not None:
            _PROCESSES[self.job_id] = self._process
        return out

    def __setstate__(self, state: tp.Any) -> None:
        # Restore instance attributes
        self.__dict__.update(state)
        # recover process if it still exists
        self._process = _PROCESSES.get(self.job_id, None)


class LocalJobEnvironment(job_environment.JobEnvironment):
    _env = {
        "job_id": "SUBMITIT_LOCAL_JOB_ID",
        "num_tasks": "SUBMITIT_LOCAL_NTASKS",
        "num_nodes": "SUBMITIT_LOCAL_JOB_NUM_NODES",
        "node": "SUBMITIT_LOCAL_NODEID",
        "global_rank": "SUBMITIT_LOCAL_GLOBALID",
        "local_rank": "SUBMITIT_LOCAL_LOCALID",
    }

    def _requeue(self, countdown: int) -> None:
        jid = self.job_id
        logger.get_logger().info(f"Requeued job {jid} ({countdown} remaining timeouts)")
        sys.exit(LOCAL_REQUEUE_RETURN_CODE)  # should help noticing if need requeuing


class LocalExecutor(core.PicklingExecutor):
    """Local job executor
    This class is used to hold the parameters to run a job locally.
    In practice, it will create a bash file in the specified directory for each job,
    and pickle the task function and parameters. At completion, the job will also pickle
    the output. Logs are also dumped in the same directory.

    The submission file spawn several processes (one per task), with a timeout.


    Parameters
    ----------
    folder: Path/str
        folder for storing job submission/output and logs.

    Note
    ----
    - be aware that the log/output folder will be full of logs and pickled objects very fast,
      it may need cleaning.
    - use update_parameters to specify custom parameters (n_gpus etc...).
    """

    job_class = LocalJob

    def __init__(
        self, folder: tp.Union[str, Path],
        max_num_timeout: int = 3,
        max_pickle_size_gb: float = 1.0,
        python: tp.Optional[str] = None
    ) -> None:
        super().__init__(
            folder,
            max_pickle_size_gb=max_pickle_size_gb,
            max_num_timeout=max_num_timeout,
        )
        self.python = shlex.quote(sys.executable) if python is None else python
        # preliminary check
        indep_folder = utils.JobPaths.get_first_id_independent_folder(self.folder)
        indep_folder.mkdir(parents=True, exist_ok=True)

    @classmethod
    def _valid_parameters(cls) -> tp.Set[str]:
        """Parameters that can be set through update_parameters"""
        return {"setup"}

    def _internal_update_parameters(self, **kwargs: tp.Any) -> None:
        """Update the parameters of the Executor.

        Valid parameters are:
        - timeout_min (float)
        - gpus_per_node (int)
        - visible_gpus (Sequence[int])
        - tasks_per_node (int)
        - nodes (int). Must be 1 if specified
        - signal_delay_s (int): signal (lately: USR2) delay before timeout

        Other parameters are ignored
        """
        if kwargs.get("nodes", 0) > 1:
            raise ValueError("LocalExecutor can use only one node. Use nodes=1")
        gpus_requested = kwargs.get("gpus_per_node", 0)
        visible_gpus = kwargs.get("visible_gpus", ())
        if not isinstance(visible_gpus, tp.Sequence):
            raise ValueError(f"Provided visible_gpus={visible_gpus} is not an instance of Sequence.")
        if not all(isinstance(x, int) for x in visible_gpus):
            raise ValueError(f"Provided visible_gpus={visible_gpus} contains an element that is not an int.")
        if len(visible_gpus) > 0 and gpus_requested > len(visible_gpus):
            raise ValueError(
                f"{gpus_requested} gpus requested, but only {visible_gpus} were specified visible."
            )
        super()._internal_update_parameters(**kwargs)

    def _submit_command(self, command: str) -> LocalJob[R]:
        # Override this, because the implementation is simpler than for clusters like Slurm
        # Only one node is supported for local executor.
        ntasks = self.parameters.get("tasks_per_node", 1)
        n_gpus = self.parameters.get("gpus_per_node", 0)
        visible_gpus = self.parameters.get("visible_gpus", ())
        gpus = range(n_gpus) if visible_gpus == () else visible_gpus[:n_gpus]
        process = start_controller(
            folder=self.folder,
            command=command,
            tasks_per_node=1,
            cuda_devices=",".join(str(k) for k in gpus),
            timeout_min=self.parameters.get("timeout_min", 2.0),
            signal_delay_s=self.parameters.get("signal_delay_s", 30),
            stderr_to_stdout=self.parameters.get("stderr_to_stdout", False),
            setup=self.parameters.get("setup", ()),
        )
        job: LocalJob[R] = LocalJob(
            folder=self.folder, job_id=str(process.pid), process=process, tasks=list(range(1))
        )
        return job

    @property
    def _submitit_command_str(self) -> str:
        ntasks = self.parameters.get("tasks_per_node", 1)
        if ntasks == 1:
            run_cmd = "python"
        else:
            run_cmd = f"mpiexec --oversubscribe --allow-run-as-root -n {ntasks} python3"
        return " ".join(
            [run_cmd, "-u -m submitit.core._submit", shlex.quote(str(self.folder))]
        )

    def _num_tasks(self) -> int:
        nodes: int = 1
        tasks_per_node: int = self.parameters.get("tasks_per_node", 1)
        return nodes * tasks_per_node

    def _make_submission_file_text(self, command: str, uid: str) -> str:
        return ""

    @staticmethod
    def _get_job_id_from_submission_command(string: tp.Union[bytes, str]) -> str:
        # Not used, but need an implementation
        return "0"

    def _make_submission_command(self, submission_file_path: Path) -> tp.List[str]:
        # Not used, but need an implementation
        return []


def start_controller(
    folder: Path,
    command: str,
    tasks_per_node: int = 1,
    cuda_devices: str = "",
    timeout_min: float = 5.0,
    signal_delay_s: int = 30,
    stderr_to_stdout: bool = False,
    setup: tp.Sequence[str] = (),
) -> "subprocess.Popen['bytes']":
    """Starts a job controller, which is expected to survive the end of the python session."""
    env = dict(os.environ)
    env.update(
        SUBMITIT_LOCAL_NTASKS=str(tasks_per_node),
        SUBMITIT_LOCAL_COMMAND=command,
        SUBMITIT_LOCAL_TIMEOUT_S=str(int(60 * timeout_min)),
        SUBMITIT_LOCAL_SIGNAL_DELAY_S=str(int(signal_delay_s)),
        SUBMITIT_LOCAL_NODEID="0",
        SUBMITIT_LOCAL_JOB_NUM_NODES="1",
        SUBMITIT_STDERR_TO_STDOUT="1" if stderr_to_stdout else "",
        SUBMITIT_EXECUTOR="local",
        CUDA_VISIBLE_DEVICES=cuda_devices,
        SUBMITIT_LOCAL_WITH_SHELL="1" if setup else "",
    )
    # The LocalJob will be responsible to polling and ending this process.
    # pylint: disable=consider-using-with
    proc_cmd: tp.Any = [sys.executable, "-m", "submitit.local._local", str(folder)]
    need_shell = bool(setup)
    if need_shell:
        proc_cmd = " && ".join(list(setup) + [shlex.join(proc_cmd)])
    process = subprocess.Popen(proc_cmd, shell=need_shell, env=env)
    return process


class Controller:
    """This controls a job:
    - instantiate each of the tasks
    - sends timeout signal
    - stops all tasks if one of them finishes
    - cleans up the tasks/closes log files when deleted
    """

    # pylint: disable=too-many-instance-attributes

    def __init__(self, folder: Path):
        self.ntasks = int(os.environ["SUBMITIT_LOCAL_NTASKS"])
        self.command = shlex.split(os.environ["SUBMITIT_LOCAL_COMMAND"])
        self.timeout_s = int(os.environ["SUBMITIT_LOCAL_TIMEOUT_S"])
        self.signal_delay_s = int(os.environ["SUBMITIT_LOCAL_SIGNAL_DELAY_S"])
        self.stderr_to_stdout = bool(os.environ["SUBMITIT_STDERR_TO_STDOUT"])
        self.tasks: tp.List[subprocess.Popen] = []  # type: ignore
        self.stdouts: tp.List[tp.IO[tp.Any]] = []
        self.stderrs: tp.List[tp.IO[tp.Any]] = []
        with_shell = bool(os.environ["SUBMITIT_LOCAL_WITH_SHELL"])
        self.pid = str(os.getppid() if with_shell else os.getpid())
        self.folder = Path(folder)
        signal.signal(signal.SIGTERM, self._forward_signal)  # type: ignore

    # pylint:disable=unused-argument
    def _forward_signal(self, signum: signal.Signals, *args: tp.Any) -> None:
        for task in self.tasks:
            try:
                task.send_signal(signum)  # sending kill signal to make sure everything finishes
            except Exception:
                pass

    def start_tasks(self) -> None:
        self.folder.mkdir(exist_ok=True)
        paths = [utils.JobPaths(self.folder, self.pid, k) for k in range(self.ntasks)]
        self.stdouts = [p.stdout.open("a") for p in paths]
        self.stderrs = self.stdouts if self.stderr_to_stdout else [p.stderr.open("a") for p in paths]
        for k in range(self.ntasks):
            env = dict(os.environ)
            env.update(
                SUBMITIT_LOCAL_LOCALID=str(k), SUBMITIT_LOCAL_GLOBALID=str(k), SUBMITIT_LOCAL_JOB_ID=self.pid
            )
            self.tasks.append(
                subprocess.Popen(  # pylint: disable=consider-using-with
                    self.command,
                    shell=False,
                    env=env,
                    stderr=self.stderrs[k],
                    stdout=self.stdouts[k],
                    encoding="utf-8",
                )
            )

    def kill_tasks(self) -> None:
        # try and be progressive in deletion...
        for sig in [signal.SIGINT, signal.SIGKILL]:
            self._forward_signal(sig)
            # if one is still alive after sigterm and sigint, try sigkill after 1s
            if sig == signal.SIGINT and any(t.poll() is None for t in self.tasks):
                time.sleep(0.001)
                if any(t.poll() is None for t in self.tasks):
                    time.sleep(1.0)  # wait a bit more
        self.tasks = []
        files = self.stdouts + self.stderrs
        self.stdouts, self.stderrs = [], []  # remove all instance references
        for f in files:
            f.close()

    def wait(self, freq: int = 24) -> tp.Sequence[tp.Optional[int]]:
        """Waits for all tasks to finish or to time-out.

        Returns
        -------
        Sequence[Optional[int]]:
            Exit codes of each task.
            Some tasks might still have not exited, but they will have received the "timed-out" signal.
        """
        assert self.tasks, "Nothing to do!"
        timeout = freq * self.timeout_s
        almost_timeout = freq * (self.timeout_s - self.signal_delay_s)

        # safer to keep a for loop :)
        for step in range(timeout):
            exit_codes = [t.poll() for t in self.tasks]
            if all(e is not None for e in exit_codes):
                return exit_codes

            if step == almost_timeout:
                self._forward_signal(LocalJobEnvironment._usr_sig())

            time.sleep(1.0 / freq)
        return [t.poll() for t in self.tasks]

    def run(self, max_retry: int = 6) -> None:
        # max_retry is a safety measure, the submission also have a timeout_countdown,
        # and will fail if it times out too many times.
        for _ in range(max_retry):
            try:
                self.start_tasks()
                exit_codes = self.wait()
                requeue = any(e == LOCAL_REQUEUE_RETURN_CODE for e in exit_codes)
                if not requeue:
                    break
            finally:
                self.kill_tasks()
