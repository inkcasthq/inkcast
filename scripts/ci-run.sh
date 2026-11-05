#!/usr/bin/env bash
# Run a CI command; on failure, put why it failed into ONE GitHub annotation. GitHub shows at most 10 error
# annotations per step, so the message is one multi-line text (issue #550):
#   - the first lines above the tail that look like a cause (at most 15, and only in the room the tail leaves),
#     since npm prints its cause first and usage text after;
#   - a separator;
#   - the last 25 lines, as before, since Python, pytest and unittest print their cause last.
# With no such line above the tail, the message is the last 25 lines alone. The step log keeps all of it.
#   scripts/ci-run.sh <label> <command> [args...]
set -uo pipefail
label="$1"; shift
log="$(mktemp)"
trap 'rm -f "$log"' EXIT
"$@" 2>&1 | tee "$log"
status=${PIPESTATUS[0]}
if [ "$status" -ne 0 ]; then
  # The runner reads a workflow command only at the start of a line, so end a last line that has no newline.
  [ -s "$log" ] && [ "$(tail -c 1 "$log" | wc -l)" -eq 0 ] && echo
  # NUL bytes are dropped, as the command substitution dropped them before #550: mawk would print them into the
  # command, one-true-awk ends a line at one, and busybox awk splits a line there.
  # LC_ALL=C: awk works on bytes, and units() counts the UTF-16 units the runner counts.
  LC_ALL=C tr -d '\000' < "$log" | CI_RUN_TITLE="${label} failed (exit ${status})" LC_ALL=C awk '
    # The runner keeps the first 4096 characters of a message and drops the rest
    # (actions/runner v2.338.2, src/Runner.Worker/ExecutionContext.cs, _maxIssueMessageLength). The REST API allows
    # 64 KB (https://docs.github.com/en/rest/checks/runs#create-a-check-run, annotations[].message), and the
    # workflow command has no line limit (https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-commands).
    BEGIN { limit = 4096; tail = 25; causes = 15
            mark = "----- cut here to fit the annotation; the step log has every line -----" }
    function is_cause(s) {
      return s ~ /^npm error +[^ ]/ || s ~ /(Invalid|Missing): / ||        # npm, without its bare "npm error" lines
             s ~ /^Traceback \(most recent call last\)/ ||                  # Python: an exception line starts the line,
             s ~ /^[A-Za-z0-9_.]*(Error|Exception): / ||                    # so a log line that names one is not a cause
             s ~ /^[A-Za-z0-9_.]*(Error|Exception)$/ ||
             s ~ /^FAILED/ || s ~ /^(FAIL|ERROR): / ||                      # pytest, unittest
             s ~ /^(fatal|error): / || s ~ /: (fatal|error): /              # git, the shell, compilers ("f.c:1: error:")
    }
    function continuation(c) { return c >= "\200" && c < "\300" }   # inside a UTF-8 character
    # The UTF-16 units the runner counts once it decodes s. A byte outside a well-formed UTF-8 character counts one,
    # never fewer than the U+FFFD the decoder puts in its place.
    function units(s,   u) {
      u = length(s)
      u -= 2 * gsub(/\360[\220-\277][\200-\277][\200-\277]|\364[\200-\217][\200-\277][\200-\277]/, ".", s)
      u -= 2 * gsub(/[\361-\363][\200-\277][\200-\277][\200-\277]/, ".", s)
      u -= 2 * gsub(/\340[\240-\277][\200-\277]|\355[\200-\237][\200-\277]/, ".", s)
      u -= 2 * gsub(/[\341-\354\356\357][\200-\277][\200-\277]/, ".", s)
      return u - gsub(/[\302-\337][\200-\277]/, ".", s)
    }
    # Escaped as @actions/core escapes them (packages/core/src/command.ts): % first, then CR and LF; a property
    # also escapes : and ,.
    function data(s) { gsub(/%/, "%25", s); gsub(/\r/, "%0D", s); gsub(/\n/, "%0A", s); return s }
    function property(s) { s = data(s); gsub(/:/, "%3A", s); gsub(/,/, "%2C", s); return s }
    {   # keep only the last lines, and the causes among the lines that leave them, so memory stays small
      if (NR > tail && n < causes && is_cause(last[NR % tail])) cause[++n] = last[NR % tail]
      last[NR % tail] = $0
    }
    END {
      first = NR > tail ? NR - tail + 1 : 1
      msg = ""; for (i = first; i <= NR; i++) msg = msg last[i % tail] "\n"
      sep = "----- the last " (NR - first + 1) " of " NR " lines -----\n"
      room = limit - units(msg) - length(sep)   # the causes never cost the tail a line
      head = ""; for (i = 1; i <= n && units(head) + units(cause[i]) < room; i++) head = head cause[i] "\n"
      if (head != "") msg = head sep msg
      if (units(msg) > limit) {   # the tail alone is too long: cut its middle, keeping its very last lines
        keep = limit - length(mark) - 2   # bytes, which are never fewer than units
        h = int(keep / 2); while (h > 0 && continuation(substr(msg, h + 1, 1))) h--
        t = length(msg) - (keep - h) + 1; while (continuation(substr(msg, t, 1))) t++
        msg = substr(msg, 1, h) "\n" mark "\n" substr(msg, t)
      }
      print "::error title=" property(ENVIRON["CI_RUN_TITLE"]) "::" data(msg)
    }'
fi
exit "$status"
