import os
import glob
import time
import datetime
import torch
import pickle
import hydra
import logging
import torchfly
from typing import Any, List, Dict, Iterator, Tuple

logger = logging.getLogger(__name__)


class Checkpointer:
    """
    Attributes:
        num_checkpoints_to_keep: Total number of checkpoints to keep
        keep_checkpoint_every_num_seconds: Keep checkpoints every x number of seconds without removing them
        storage_dir: Location to store the checkpoints
    """
    def __init__(
        self,
        sync_every_save: bool = True,
        num_checkpoints_to_keep: int = 1000,
        keep_checkpoint_every_num_seconds: float = 3600,
        storage_dir: str = "Checkpoints"
    ):
        self.sync_every_save = sync_every_save
        self.num_checkpoints_to_keep = num_checkpoints_to_keep
        self.keep_checkpoint_every_num_seconds = keep_checkpoint_every_num_seconds
        self.storage_dir = storage_dir
        self._saved_checkpoint_paths: List[Tuple[float, str]] = []
        self._last_checkpoint_time = datetime.datetime.now()
        self.background_tasks = []

        os.makedirs(storage_dir, exist_ok=True)

    def save_checkpoint(self, stamp: str, states: Dict[str, Any]) -> None:
        """
        Args:
            stamp: A string to identify the checkpoint. It can just be the epoch number
            states: A dictionary to store all necessary information for later restoring
        """
        # synchronize background tasks
        if self.sync_every_save:
            for ray_obj in self.background_tasks:
                torchfly.check_async_status(ray_obj)
                logger.debug("Waiting for history job to finish!")
            self.background_tasks = []

        checkpoint_path = os.path.join(self.storage_dir, f"{stamp}_state.pth")

        # save the states
        ray_obj = torchfly.async_save(states, checkpoint_path)
        self.background_tasks.append(ray_obj)

        # remove the old one
        if self.num_checkpoints_to_keep >= 0:
            self._saved_checkpoint_paths.append((datetime.datetime.now(), checkpoint_path))

            if len(self._saved_checkpoint_paths) > self.num_checkpoints_to_keep:
                path_to_remove = self._saved_checkpoint_paths.pop(0)

                # check time requirement
                remove_path = True
                if self.keep_checkpoint_every_num_seconds is not None:
                    save_time = path_to_remove[0]
                    time_since_checkpoint_kept = (save_time - self._last_checkpoint_time).total_seconds()
                    if time_since_checkpoint_kept > self.keep_checkpoint_every_num_seconds:
                        # We want to keep this checkpoint.
                        remove_path = False
                        self._last_checkpoint_time = save_time

                if remove_path:
                    for fname in path_to_remove[1:]:
                        if os.path.isfile(fname):
                            logger.debug(f"Removing {fname}!")
                            os.remove(fname)

    def restore_latest_checkpoint(self) -> [Dict, None]:
        """
        Returns:
            state_dict: return the checkpoint's state dict. None if there is nothing.
        """
        files = glob.glob(os.path.join(self.storage_dir, "*_state.pth"))
        sorted_files = sorted(files, key=os.path.getctime, reverse=True)

        for latest_file in sorted_files:
            latest_file_path = os.path.join(self.storage_dir, latest_file)
            try:
                checkpoint = torch.load(latest_file_path, map_location="cpu")
                checkpoint["file_path"] = latest_file_path
                logger.info(f"Loading checkpoint {latest_file_path}")
                return checkpoint
            except (pickle.UnpicklingError, RuntimeError):
                # skip and remove the corrupted files
                logger.info(f"Checkpoint {latest_file_path} is corrupted. It will be deleted.")
                os.remove(latest_file_path)
                continue
        # if found files but failed to load them
        if len(files) > 0:
            logger.info("Fail to retrieve the old checkpoints!")
        # if there is nothing to restore, return None
        return None

    def state_dict(self):
        states = {
            "_saved_checkpoint_paths": [(str(saved_time), path) for saved_time, path in self._saved_checkpoint_paths],
            "_last_checkpoint_time": str(self._last_checkpoint_time)
        }
        return states

    def load_state_dict(self, states: Dict[str, Any]):
        self._saved_checkpoint_paths = [
            (datetime.datetime.strptime(saved_time, '%Y-%m-%d %H:%M:%S.%f'), path)
            for saved_time, path in states["_saved_checkpoint_paths"]
        ]
        self._last_checkpoint_time = datetime.datetime.strptime(states["_last_checkpoint_time"], '%Y-%m-%d %H:%M:%S.%f')