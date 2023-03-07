from enum import Enum
from os import PathLike
from pathlib import Path
from typing import NamedTuple, Dict, Optional, Any, List, Set, Union, Tuple

from huggingface_hub import cached_download, hf_hub_url
from iree.compiler import InputType

from . import _TIR_HUB_USER_AGENT
from tomlkit import load as load_toml


class TirTarget(str, Enum):
    INTERPRETED_CPU = "vmvx"
    COMPILED_CPU = "llvm-cpu"
    COMPILED_GPU = "vulkan"
    COMPILED_CUDA = "cuda"
    COMPILED_ROCM = "rocm"

    def to_tf_compiler_value(self) -> str:
        if self == TirTarget.INTERPRETED_CPU:
            return "iree_vmvx"
        elif self == TirTarget.COMPILED_CPU:
            return "iree_llvmcpu"
        elif self == TirTarget.COMPILED_GPU:
            return "iree_vulkan"
        else:
            raise ValueError(f"Target {self} is not compatible with tf compiler.")

    def to_driver(self) -> str:
        if self == TirTarget.COMPILED_CUDA:
            return "cuda"
        elif self == TirTarget.COMPILED_ROCM:
            return "rocm"
        elif self == TirTarget.COMPILED_GPU:
            return "vulkan"
        else:
            return "local-task"


TirFrontendInfo = NamedTuple("TirFrontendInfo", [("name", str), ("dialect", InputType)])


class TirFrontend(Enum):
    PYTORCH = TirFrontendInfo("pytorch", InputType.TM_TENSOR)
    TENSORFLOW = TirFrontendInfo("tensorflow", InputType.MHLO)
    TFLITE = TirFrontendInfo("tflite", InputType.TOSA)
    JAX = TirFrontendInfo("jax", InputType.XLA)


class iree_cl:
    __slots__ = ("_position", "_id", "_value")

    @staticmethod
    def from_sequence(parts: Union[Tuple[int, str], Tuple[int, str, Any]]) -> "iree_cl":
        if len(parts) == 2:
            return iree_cl(int(parts[0]), str(parts[1]))
        elif len(parts) == 3:
            return iree_cl(int(parts[0]), str(parts[1]), parts[2])
        else:
            raise ValueError(f"Unable to create iree_cl from {len(parts)} only pair and triplet are supported.")

    def __init__(self, position: int, id: str, value: Optional[Any] = None):
        self._position = position
        self._id = id
        self._value = value

    @property
    def id(self) -> str:
        return self._id

    @property
    def position(self) -> int:
        return self._position

    @property
    def value(self) -> Any:
        return self._value

    def __hash__(self):
        return hash(self._id)

    def __str__(self):
        if self._value:
            return f"{self.id}={self.value}"
        else:
            return self.id

    def __repr__(self):
        return f"{str(self)}@{self.position}"


class TirConfig:

    __slots__ = ("_flags", "_mlir_output_path")

    @staticmethod
    def parse_flags(flags: List[str]) -> Set[iree_cl]:
        return set(map(lambda f: iree_cl.from_sequence((f[0], ) + f[1:]), enumerate(flags)))

    def __init__(self, flags: Optional[List[str]] = None):

        if flags:
            self._flags = TirConfig.parse_flags(flags)
        else:
            self._flags = set()

    def with_debug_flags(self) -> "TirConfig":
        """
        Include the MLIR debugging flags to the compiler invocation
        :return:
        """

        self._flags.add(iree_cl(-1000, "--mlir-elide-elementsattrs-if-larger", 1))
        self._flags.add(iree_cl(-1000, "--mlir-print-ir-before-all"))
        return self

    def with_cpu_target(self, target: str = None) -> "TirConfig":
        """
        Define the target CPU LLVM will compile for
        :param target: LLVM cpu target (ex: skylake-avx512, icelake-server, etc.)
        :return:
        """
        if target is None:
            target = "host"

        self._flags.add(iree_cl(-1, "--iree-llvm-target-cpu-features", target))
        return self

    def with_gpu_target(self, target_sm: str) -> "TirConfig":
        """
        Define the target GPU SM LLVM will compile for
        :param target_sm: Device capabilities version (SM in Nvidia's vocab)
        :return:
        """
        # TODO: Improve "target_sm" will not speak to many people
        # example sm_80 for Ampere
        self._flags.add(iree_cl(-1, "--iree-hal-cuda-llvm-target-arch", f"sm_{target_sm.replace('.', '')}"))
        return self

    def with_mlir_ir_output(self, path: PathLike) -> "TirConfig":
        """
        Save intermediate representations (IR) at the specified path
        :param path: Path where to store MLIR IR.
        :return:
        """
        self._mlir_output_path = path
        return self

    def save_ir(self, mlir_module: str):
        if self._mlir_output_path:
            with open(self._mlir_output_path, "w", encoding="utf-8") as f:
                f.write(mlir_module)

    def get_compiler_args(self) -> List[str]:
        """
        Return the flags to be forwarded to IREE's compiler
        :return:
        """

        return [str(flag) for flag in sorted(self._flags, key=lambda f: f.position)]

    def get_tuned_parameters_for_device(self, device: TirTarget) -> Optional[Dict[str, Any]]:
        """
        Retrieve tuned parameters for the specified device or a default config if the device doesn't have a tuned
        configuration.
        :param device: The device we are trying to tune for.
        :return:
        """
        raise NotImplementedError()

    def get_tunable_parameters(self, device: TirTarget) -> Optional[Dict[str, Any]]:
        """
        Retrieve which parameters from this configuration can be tuned
        :param device: The device we are trying to tune for.
        :return:
        """
        raise NotImplementedError()

    def register_additional_parameters(self, name: str, value: Optional[Any] = None, index: int = -1) -> 'TirConfig':
        """

        :param name: The parameter name to append.
        :param value: The value for parameter if any.
        :param index: Where in the pipeline we should insert this parameter (default -1: at the end).
        :return:
        """
        self._flags.add(iree_cl(index, name, value))
        return self


class TirConfigStore:

    @staticmethod
    def from_hub(repo_id: str, filename: str = "tir-config.store", branch: str = "tir") -> "TirConfigStore":
        target_repo_url = hf_hub_url(repo_id=repo_id, filename=filename, revision=branch)
        local_file_path = cached_download(target_repo_url, user_agent=_TIR_HUB_USER_AGENT)

        if local_file_path is not None:
            local_file_path = Path(local_file_path)
            return TirConfigStore.from_file(local_file_path, repo_id)

        raise ValueError(f"Unable to find {filename} at {target_repo_url}.")

    @staticmethod
    def from_file(filepath: PathLike, repo_id: str) -> "TirConfigStore":
        with open(filepath, "r", encoding="utf-8") as store_f:
            store = load_toml(store_f)
            return TirConfigStore(repo_id, store)

    def __init__(self, repo_id: str, store: Dict[str, Dict[str, Any]]):
        self._repo_id = repo_id
        self._store = store

    def __getitem__(self, item):
        return self._store[item]


