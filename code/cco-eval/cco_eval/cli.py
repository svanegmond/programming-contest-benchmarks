"""CLI entry point: argparse wiring for all subcommands."""

from __future__ import annotations

import argparse

from cco_eval.commands_cco import command_index, command_list, command_run
from cco_eval.corpus_cco import DEFAULT_INDEX_PATH
from cco_eval.providers import DEFAULT_OLLAMA_URL


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cco-eval", description="Run LLM evals against CCO problems.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    index_parser = subparsers.add_parser("index", help="Build a CCO problem index from a captured corpus.")
    index_parser.add_argument("--corpus", required=True, help="Path to the statements directory.")
    index_parser.add_argument("--index-path", help=f"Index output path. Defaults to {DEFAULT_INDEX_PATH}.")
    index_parser.set_defaults(func=command_index)

    list_parser = subparsers.add_parser("list", help="List discovered problem IDs.")
    list_parser.add_argument("--corpus", required=True, help="Path to the statements directory.")
    list_parser.add_argument("--json", action="store_true", help="Print JSON output.")
    list_parser.set_defaults(func=command_list)

    run_parser = subparsers.add_parser("run", help="Generate, compile, and score one problem.")
    run_parser.add_argument("--corpus", required=True, help="Path to the statements directory.")
    run_parser.add_argument("--provider", choices=["ollama", "anthropic"], default="ollama", help="Model provider.")
    run_parser.add_argument("--model", required=True, help="Ollama model name, such as qwen2.5:14b.")
    run_parser.add_argument("--problem", required=True, help="Problem ID, such as 2024-p1.")
    run_parser.add_argument("--out", required=True, help="Run output directory.")
    run_parser.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL, help="Base Ollama URL.")
    run_parser.add_argument("--ollama-timeout", type=float, default=180.0, help="Ollama request timeout in seconds.")
    run_parser.add_argument("--anthropic-api-key", help="Anthropic API key. Defaults to ANTHROPIC_API_KEY.")
    run_parser.add_argument("--anthropic-timeout", type=float, default=180.0, help="Anthropic request timeout in seconds.")
    run_parser.add_argument("--max-tokens", type=int, default=4096, help="Maximum model output tokens.")
    run_parser.add_argument("--compile-timeout", type=float, default=10.0, help="py_compile timeout in seconds.")
    run_parser.add_argument("--test-timeout", type=float, default=5.0, help="Per-test timeout in seconds.")
    run_parser.add_argument("--max-official-tests", type=int, default=0, help="Official cases to run after samples.")
    run_parser.add_argument("--max-input-bytes", type=int, default=2_000_000, help="Skip tests with larger inputs.")
    run_parser.add_argument("--max-output-bytes", type=int, default=2_000_000, help="Skip tests with larger expected outputs.")
    run_parser.add_argument("--mock-response", help="Use literal text or a file path as the model response.")
    run_parser.add_argument("--dry-run", action="store_true", help="Use a built-in compileable mock response.")
    run_parser.set_defaults(func=command_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))
