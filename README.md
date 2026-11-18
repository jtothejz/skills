# skills

Small Claude skills for everyday chores. Most aren't big workflows. They're
the jobs you do every week and re-learn every time, the ones you wouldn't
think to hand an agent.

| Skill | What it does |
|---|---|
| [print-run](plugins/print-run) | "Print 40 postcards and 10 flyers": works out the paper, tells you how to stack the tray, waits for you to load it, prints in order with the right settings, tells you when it's done. |

## Install

**Claude Code:** add this repo as a plugin marketplace once, then install the
skills you want:

```
/plugin marketplace add jtothejz/skills
/plugin install print-run@jtothez-skills
```

**Claude apps (claude.ai, Desktop):** each skill has a `.skill` file on the
[Releases](https://github.com/jtothejz/skills/releases) page. Download it and
upload it under Settings → Capabilities → Skills.

## Layout

`plugins/<name>/skills/<name>/` holds each skill (`SKILL.md`, scripts,
references). `plugins/<name>/.claude-plugin/plugin.json` and
`.claude-plugin/marketplace.json` make the repo installable from Claude Code.

MIT license.
