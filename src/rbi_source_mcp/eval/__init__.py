"""Hand-labeled compliance retrieval eval set + runner.

Locks the v0.5+ retrieval-quality contract: known compliance clauses must
surface specific MD provisions in the top-5 results from `check_compliance`.
Every corpus-release run (daily diff + monthly full) runs this gate; failures fail the build.

This is REG-4 from the eng review's locked regression-test set.
"""
