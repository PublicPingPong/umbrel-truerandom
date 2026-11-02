"""NUR FÜR TESTS: imitiert die Ausgabe von `infnoise --debug` mit os.urandom.

Wird nie im Image verwendet. Label im Dienst dann z. B. SOURCE_LABEL=TEST-urandom.
"""
import os, sys, time
raw = "--raw" in sys.argv
bits = 0
out = sys.stdout.buffer
try:
    while True:
        out.write(os.urandom(8192)); out.flush()
        bits += 8192 * 8 * 2
        if bits % (1 << 20) < 8192 * 16:
            sys.stderr.write(f"Generated {bits} bits.  OK to use data.  Estimated entropy per bit: 0.866{bits % 7}12, estimated K: 1.823767\n")
            sys.stderr.write("num1s:49.9%, even misfires:0.13%, odd misfires:0.12%\n"); sys.stderr.flush()
        time.sleep(0.05)
except (BrokenPipeError, KeyboardInterrupt):
    pass
