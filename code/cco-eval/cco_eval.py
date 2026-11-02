#!/usr/bin/env python3
"""Thin shim — delegates to the cco_eval package."""

from cco_eval.cli import main

raise SystemExit(main())
