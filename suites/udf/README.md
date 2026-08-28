# The UDF suite

No standard benchmark measures the thing firepanda is actually about, so this one is ours to define, and it is defined to be fair rather than flattering.

Status: not wired up. `tools/queries.py` knows about db-benchmark and TPC-H only, and this suite waits on the expression API and the Python binding. What follows is the shape it will take.

Because the queries here are ours, this suite carries a burden the other two do not. db-benchmark and TPC-H are credible because somebody else wrote them. This one has to earn it, and the fifth column below is most of how.

## The claim being tested

Every fast dataframe library today is a fast engine in one language with a Python veneer. The moment a user writes a function the library did not anticipate, they leave the fast language and enter the slow one. That is why `df.apply(lambda ...)` falls off a cliff in pandas, and it is structural rather than a missing optimization.

Mojo removes the boundary. The library, the kernels and the user's own hot loop are all the same language and they all compile. This suite measures whether that is worth anything.

## The design

Six operations of increasing complexity, from a two-term arithmetic expression up to a branch-heavy row-wise computation with string handling that no expression API covers.

Each expressed five ways:

| Column | What it is |
|---|---|
| pandas, vectorized | The expression form, where one exists. Some of the six have no vectorized form, which is the point. |
| pandas, `.apply` | A Python lambda. The cliff. |
| firepanda, expression | Our expression API, no user function involved. |
| firepanda, Mojo function | The user's own function compiled into the pipeline. The claim. |
| firepanda, Python lambda through the binding | The slow path, at its real cost. |

Polars gets both of its forms too, the expression and `map_elements`, because Polars users hit the same cliff and pretending only pandas has it would be a cheat.

## Why the fifth column exists

It is easy to build a UDF benchmark where the Mojo column wins by 100x and quietly omit that a Python user calling `df.apply(lambda ...)` gets the fourth-place number.

Most people who install firepanda will arrive through `pip install` and write Python. For them a Python callable is still a Python callable, and the language argument does not pay off. It pays off for people writing Mojo. The fifth column is what keeps this table honest about who benefits.

If that column is embarrassing, it goes in the table anyway, and the migration guide says where the cliff is.

## Reported

Timing with the full spread, peak resident set, and rows per second, so the six operations are comparable to each other rather than only within a row.
