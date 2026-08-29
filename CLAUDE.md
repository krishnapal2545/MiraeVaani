# Development Rules

## Core Principle

Write the minimum amount of code necessary.

Prefer simple, readable, maintainable solutions over clever or verbose implementations.

Before writing new code:
1. Inspect the existing code.
2. Search the repository for existing utilities, helpers, services, components, and patterns.
3. Check whether an existing library can solve the problem.
4. Reuse existing code whenever possible.

## Libraries

DO NOT implement functionality from scratch if a mature library already exists.

Prefer existing libraries for:
- validation
- authentication
- HTTP clients
- date/time handling
- parsing
- logging
- database utilities
- retries
- caching
- serialization
- file handling
- common algorithms
- UI components

Before adding a dependency, check package.json / requirements.txt / existing dependencies first.

Do not add a new dependency unless it provides a clear benefit.

## Minimal Code

Always choose the smallest reasonable implementation.

DO NOT:
- create unnecessary abstractions
- create unnecessary classes
- create unnecessary interfaces
- create unnecessary wrapper functions
- duplicate existing utilities
- add boilerplate
- add speculative features
- over-engineer simple problems
- create files unless necessary

Prefer:
- existing functions
- existing services
- existing components
- existing libraries
- straightforward code

## Changes

IMPORTANT: Do not modify unrelated code.

Only change files required to solve the requested problem.

Do not refactor surrounding code unless explicitly requested.

Do not rename variables, functions, files, APIs, or components unless necessary.

Do not change architecture unless explicitly requested.

## Before Editing

Before making changes, explain:

1. What is wrong.
2. Which files need to change.
3. What approach you will take.
4. Whether an existing library/code can be reused.

Then WAIT for approval before making modifications.

## Code Quality

Code should be:
- simple
- readable
- predictable
- idiomatic
- easy for another developer to understand

Avoid clever one-liners when they reduce readability.

Comments should explain WHY, not WHAT.

## Completion

After implementing:
- show the files changed
- summarize the changes
- mention any new dependencies
- mention anything that still needs attention

Do not make additional improvements that were not requested.

## Minimal Implementation Rule

Before creating more than ~20 lines of new code, stop and reconsider:

"Can this be solved with existing code, an existing library, or a significantly simpler implementation?"

Prefer:

existing code > existing library > small custom function > new abstraction

Never generate a large framework/architecture for a small requirement.

## Repository First

NEVER immediately start writing code.

First inspect the repository.

Search for:
- similar implementations
- existing utilities
- existing dependencies
- existing API clients
- existing components
- existing patterns

If an existing solution exists, reuse it.

Do not create a second implementation of something that already exists.