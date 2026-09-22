# ChatGPT / AI-Beast execution bridge

This branch contains the guarded local execution queue.

## Job
`.chatgpt-bridge/jobs/job-*/job.json`
`.chatgpt-bridge/jobs/job-*/run.sh`

## Result
`.chatgpt-bridge/results/job-*/result.txt`
`.chatgpt-bridge/results/job-*/metadata.json`
`.chatgpt-bridge/results/job-*/exit-code.txt`
`.chatgpt-bridge/results/job-*/COMPLETE`

The AI-Beast runner writes job output continuously to an immutable local spool
outside the Git clone. GitHub publication is a retryable second phase. A local
claim is never automatically executed twice.
