# CloudMarks repository account

Use the personal GitHub account **dzuritaa** for this project and its repository,
`https://github.com/dzuritaa/cloudmarks`. Do not use the work GitHub account for
project API calls, pushes, CI or releases.

Before GitHub operations, verify the authenticated CLI identity is `dzuritaa`;
never assume the globally active account is appropriate. Git authentication and
GitHub CLI authentication can use different credentials. Verify both when fixing
an account problem, without printing or saving credential values.

Preserve the work account's saved login and other repositories' configuration.
Keep commit author settings repository-local. Never print tokens, inspect
credential stores in logs, or commit credentials. If the personal credential
needs additional OAuth scope, request reauthorization of that personal account
only; do not broaden the work account's permissions.
