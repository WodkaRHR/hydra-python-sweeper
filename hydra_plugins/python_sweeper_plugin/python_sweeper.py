# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
from dataclasses import dataclass, field

import itertools, more_itertools
import logging
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence, Tuple

from hydra.types import HydraContext
from hydra.core.config_store import ConfigStore
from hydra.core.override_parser.overrides_parser import OverridesParser
from hydra.core.override_parser.types import Override
from hydra.core.plugins import Plugins
from hydra.plugins.launcher import Launcher
from hydra.plugins.sweeper import Sweeper
from hydra.types import TaskFunction
from omegaconf import DictConfig, OmegaConf

from hydra.utils import get_method

from .utils import merge_overrides, compress_override

# IMPORTANT:
# If your plugin imports any module that takes more than a fraction of a second to import,
# Import the module lazily (typically inside sweep()).
# Installed plugins are imported during Hydra initialization and plugins that are slow to import plugins will slow
# the startup of ALL hydra applications.
# Another approach is to place heavy includes in a file prefixed by _, such as _core.py:
# Hydra will not look for plugin in such files and will not import them during plugin discovery.

log = logging.getLogger(__name__)


@dataclass
class SweeperConfig:
    _target_: str = (
        "hydra_plugins.python_sweeper_plugin.python_sweeper.PythonSweeper"
    )
    # max number of jobs to run in the same batch.
    max_batch_size: Optional[int] = None
    # python entrypoint
    entrypoints: List[str] = field(default_factory=list)
    # whether to remove duplicate configurations
    remove_duplicates: bool = False


ConfigStore.instance().store(group="hydra/sweeper", name="python", node=SweeperConfig)


class PythonSweeper(Sweeper):
    
    def __init__(self, max_batch_size: Optional[int], entrypoints: Sequence[str], remove_duplicates: bool):
        self.max_batch_size = max_batch_size
        self.entrypoints: Sequence[str] = entrypoints
        self.remove_duplicates: bool = remove_duplicates
        self.config: Optional[DictConfig] = None
        self.launcher: Optional[Launcher] = None
        self.hydra_context: Optional[HydraContext] = None
        self.job_results = None

    def setup(
        self,
        *,
        hydra_context: HydraContext,
        task_function: TaskFunction,
        config: DictConfig,
    ) -> None:
        self.config = config
        self.launcher = Plugins.instance().instantiate_launcher(
            hydra_context=hydra_context, task_function=task_function, config=config
        )
        self.hydra_context = hydra_context
        
    def __repr__(self) -> str:
        return (
            f"PythonSweeper(max_batch_size={self.max_batch_size!r}, "
            f"entrypoint={self.entrypoints!r})"
        )

    def _save_sweep_config(self):
        # Save sweep run config in top level sweep working directory
        sweep_dir = Path(self.config.hydra.sweep.dir)
        sweep_dir.mkdir(parents=True, exist_ok=True)
        OmegaConf.save(self.config, sweep_dir / "multirun.yaml")

    def _make_cli_overrides(self, cli_overrides: List[Override]) -> List[Tuple[str, str]]:
        """ Gets all overrides from the command line interface as key, value pairs. """
        lists = []
        for override in cli_overrides:
            if override.is_sweep_override():
                sweep_choices = override.sweep_string_iterator()
                key = override.get_key_element()
                sweep = [(key, val) for val in sweep_choices]
                lists.append(sweep)
            else:
                key = override.get_key_element()
                val = override.get_value_element_as_str()
                lists.append([(key, val)])
        return list(itertools.product(*lists))
                
    def _make_python_overrides(self) -> Sequence[List[List[Tuple[str, str]]]]:
        """ Gets all overrides from the python entrypoint. """
        result = [[[]]]
        for entrypoint in self.entrypoints:
            method = get_method(entrypoint)
            result.append([tuple(overrides) for overrides in method()])
        return result
    
    def _make_batches(self, cli_overrides: List[Override]) -> List[Tuple[str]]:
        """ Makes the batches. """
        batches = merge_overrides(self._make_cli_overrides(cli_overrides),
                                  *self._make_python_overrides())
        batches = [tuple(f"{key}={value}" for key, value in batch) for batch in map(compress_override, batches)]
        if self.remove_duplicates:
            batches = list(dict.fromkeys(batches)) # repsects ordering as of python 3.8
        return batches
    
    def _chunk_batches(self, batches: Sequence[Sequence[str]]) -> Iterable[Sequence[Sequence[str]]]:
            """
            Split input to chunks of up to n items each
            """
            n = self.max_batch_size
            if n is None or n == -1:
                n = len(batches)
            return more_itertools.chunked(batches, n)

    def sweep(self, arguments: List[str]) -> Any:
        assert self.config is not None
        assert self.launcher is not None
        log.info(f"{self!s} sweeping")
        log.info(f"Sweep output dir : {self.config.hydra.sweep.dir}")

        self._save_sweep_config()

        parser = OverridesParser.create()
        parsed = parser.parse_overrides(arguments)
        batches = self._make_batches(parsed)
        self.validate_batch_is_legal(batches)
        chunked_batches = list(self._chunk_batches(batches))

        returns = []
        initial_job_idx = 0
        for batch in chunked_batches:
            results = self.launcher.launch(batch, initial_job_idx=initial_job_idx)
            initial_job_idx += len(batch)
            returns.append(results)
        return returns