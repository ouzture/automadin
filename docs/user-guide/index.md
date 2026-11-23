# User guide

This guide takes you from a new installation to reliable reusable flows. You do
not need to read it front to back: complete the first-flow tutorial, then follow
the sections relevant to your target and workflow.

## Recommended path

1. [Install and complete first setup](installation.md).
2. [Create your first flow](first-flow.md).
3. Learn the [workspace](workspace.md) and [flow-building tools](building-flows.md).
4. Read the guide for your target: [browser](targets/browser.md),
   [Android](targets/android.md), or [Windows](targets/windows.md).
5. Add [values and control flow](values-and-control-flow.md) as the automation grows.
6. Turn a tested flow into a [saved run setup or schedule](run-setups-and-schedules.md).

## How Automadin is organized

An **automation flow** is an ordered tree of commands. Commands can act on the
selected target, wait for evidence, branch, repeat, call another flow, update a
variable, or stop the run. A flow is target-independent until its commands make
target-specific assumptions.

A **run setup** binds a flow to a target and stores the runtime choices needed to
launch it consistently. A **schedule** starts a saved run setup at recurring
times. The **automation library** manages the flows, run setups, template images,
and visual rules stored in your workspace.

## Conventions used here

- Menu paths appear as **Tools > Runtime Components**.
- Buttons and workspace pages appear in **bold**.
- Example names such as `Examples/Welcome check` are synthetic and safe to copy.
- Requirements marked “optional” are needed only for the related feature.
