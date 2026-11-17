# Build jobs and scheduling update

- Multiple branch-specific build jobs can be configured independently.
- New jobs and jobs migrated from the previous comma-separated branch setting are disabled by default.
- Each job has a display name, fork branch, upstream branch, interval (hours), and ISO setting.
- Enabling a job schedules its first run one full interval later.
- Disabling a job clears its next-run time and cancels a queued scheduled build; a build already running is allowed to finish.
- Manual Run now works whether the job is enabled or disabled.
- One worker serializes builds. If multiple jobs are due, they are handled in order as the worker becomes available.
- Job configuration and schedule timestamps are stored in the existing SQLite database on the persistent build_state volume.

Existing databases are migrated in place. No named volumes need to be removed. The old branch list is imported once as disabled jobs.
