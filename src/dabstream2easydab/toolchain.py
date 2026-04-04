from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parent

TOOL_ENV_VARS = {
    "edi2eti": "DABSTREAM_EDI2ETI",
    "odr-edi2edi": "DABSTREAM_ODR_EDI2EDI",
    "eti2zmq": "DABSTREAM_ETI2ZMQ",
}


def _bundled_dirs() -> list[Path]:
    return [
        PROJECT_ROOT / "tools" / "bin",
        PACKAGE_ROOT / "_tools" / "bin",
    ]


@dataclass(frozen=True)
class ToolOverrideConfig:
    edi2eti_path: str = ""
    odr_edi2edi_path: str = ""
    eti2zmq_path: str = ""

    def override_for(self, tool_name: str) -> str:
        if tool_name == "edi2eti":
            return self.edi2eti_path
        if tool_name == "odr-edi2edi":
            return self.odr_edi2edi_path
        if tool_name == "eti2zmq":
            return self.eti2zmq_path
        raise KeyError(tool_name)


@dataclass(frozen=True)
class ToolInfo:
    name: str
    path: str = ""
    available: bool = False
    source: str = "missing"
    detail: str = ""

    @property
    def display_status(self) -> str:
        if self.available:
            return f"{self.source}: {self.path}"
        return self.detail or "introuvable"


class ToolchainError(RuntimeError):
    """Raised when a required external tool cannot be resolved."""


@dataclass(frozen=True)
class Toolchain:
    edi2eti: ToolInfo
    odr_edi2edi: ToolInfo
    eti2zmq: ToolInfo

    @classmethod
    def discover(cls, overrides: ToolOverrideConfig | None = None) -> "Toolchain":
        overrides = overrides or ToolOverrideConfig()
        return cls(
            edi2eti=_resolve_tool("edi2eti", overrides.override_for("edi2eti")),
            odr_edi2edi=_resolve_tool(
                "odr-edi2edi", overrides.override_for("odr-edi2edi")
            ),
            eti2zmq=_resolve_tool("eti2zmq", overrides.override_for("eti2zmq")),
        )

    def info_for(self, tool_name: str) -> ToolInfo:
        if tool_name == "edi2eti":
            return self.edi2eti
        if tool_name == "odr-edi2edi":
            return self.odr_edi2edi
        if tool_name == "eti2zmq":
            return self.eti2zmq
        raise KeyError(tool_name)

    def command(self, tool_name: str) -> str:
        info = self.require(tool_name)
        return info.path

    def require(self, tool_name: str) -> ToolInfo:
        info = self.info_for(tool_name)
        if info.available:
            return info
        raise ToolchainError(
            f"Outil requis introuvable: {tool_name}. {info.display_status}"
        )


def _resolve_tool(tool_name: str, override_path: str) -> ToolInfo:
    cleaned_override = override_path.strip()
    if cleaned_override:
        expanded = os.path.expanduser(cleaned_override)
        return _tool_info_from_candidate(
            tool_name,
            expanded,
            source="override",
            missing_detail="chemin configure introuvable ou non executable",
        )

    env_var = TOOL_ENV_VARS[tool_name]
    env_override = os.environ.get(env_var, "").strip()
    if env_override:
        expanded = os.path.expanduser(env_override)
        return _tool_info_from_candidate(
            tool_name,
            expanded,
            source=f"env:{env_var}",
            missing_detail=f"chemin {env_var} introuvable ou non executable",
        )

    for bundled_dir in _bundled_dirs():
        candidate = bundled_dir / tool_name
        if _is_executable_file(candidate):
            return ToolInfo(
                name=tool_name,
                path=str(candidate),
                available=True,
                source="bundle",
                detail="binaire integre a l'application",
            )

    path_candidate = shutil.which(tool_name)
    if path_candidate:
        return ToolInfo(
            name=tool_name,
            path=path_candidate,
            available=True,
            source="system",
            detail="trouve dans le PATH",
        )

    bundle_list = ", ".join(str(path) for path in _bundled_dirs())
    return ToolInfo(
        name=tool_name,
        available=False,
        source="missing",
        detail=f"absent du PATH et d'aucun bundle local ({bundle_list})",
    )


def _tool_info_from_candidate(
    tool_name: str,
    candidate: str,
    source: str,
    missing_detail: str,
) -> ToolInfo:
    path = Path(candidate)
    if _is_executable_file(path):
        return ToolInfo(
            name=tool_name,
            path=str(path),
            available=True,
            source=source,
            detail="chemin configure explicitement",
        )
    return ToolInfo(
        name=tool_name,
        path=str(path),
        available=False,
        source=source,
        detail=missing_detail,
    )


def _is_executable_file(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)
