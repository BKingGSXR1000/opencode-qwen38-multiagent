# Jupiter R1 observed-failure fixes

The first fresh original Jupiter one-shot on commit dff7749 stopped in
recursive split. Two generic harness defects were demonstrated by durable
receipts.

First, the separate two-denial action budget fired before the already existing
complexity-aware Early Write deadline. D002 and D003 each consumed both normal
attempts after denied reads of missing progress or owned files, without
semantic implementation work. The redundant two-denial abort layer is removed.
The existing effective-tool deadline remains authoritative: S permits four
turns and M permits six. Its terminal implementation_direct_write_noncompliance
reason is now explicitly classified as worker behavior.

Second, split validation rejected a later child reading public/astro.js because
the file did not exist at split time, even though that child explicitly depended
on the first sibling and the first sibling declared public/astro.js in
creates_or_updates. Missing reads are now allowed only when the exact path is a
prospective output of that explicit predecessor. Unrelated missing reads remain
rejected.

No retry limits were increased and no new recovery state was added.
