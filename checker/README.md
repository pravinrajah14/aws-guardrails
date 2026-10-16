This folder demonstrates that policies with mistakes in their meaning are still able to pass Dogwood's validator. 
`build.py` is the main script, and it writes 3 policies with 13 broken versions. It validates them and then compares their verdicts.
`recently/` holds the two readings of “recently” and the histories where they disagree.
`z3_find_disagreement.py` is the solver demo. Both `sanity.py` and `schema.cedarschema` are helper files, can be skipped

`cargo install dogwood-cli`
`export DOGWOOD=$(which dogwood)`
`pip install z3-solver`
`python3 build.py`
`python3 sanity.py`
`python3 z3_find_disagreement.py`
`dogwood replay recently/reading_A_window.dw --policy-schema schema.cedarschema --trace recently/trace_fail_then_pass.log`

Result: All 13 broken policies pass dogwood validate; each gives a different verdict from the correct policy on its test history
