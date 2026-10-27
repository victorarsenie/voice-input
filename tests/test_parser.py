"""Parser tests: no audio, no Windows API. Run with the venv python."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import voice

voice._cfg = voice.load_config()
voice.load_phrases()

FAILS = []


def check(label, got, want):
    if got != want:
        FAILS.append("%s\n   got:  %r\n   want: %r" % (label, got, want))


def steps(text):
    return voice.plan_transcript(text)


# --- dictionary -----------------------------------------------------------
check("dictionary opencode", voice.apply_dictionary("open code is open coded"), "OpenCode is OpenCode")
check("dictionary utorrent", voice.apply_dictionary("you torrent crashed"), "uTorrent crashed")
check("dictionary untouched", voice.apply_dictionary("nothing to fix here"), "nothing to fix here")

# --- commands -------------------------------------------------------------
check("whole command", steps("send prompt"), [("send", "")])
check("unpunctuated repeat", steps("Send send it"), [("send", "")])
check("trailing command", steps("hello there send prompt"), [("add", "hello there"), ("send", "")])
check("leading command", steps("speak that I am not sure"), [("speak", ""), ("add", "I am not sure")])
check("single word stays prose", steps("send me the file"), [("add", "send me the file")])
check("copy stays prose", steps("copy the path"), [("add", "copy the path")])

# --- edits ----------------------------------------------------------------
check("delete one word", voice.parse_edit("remove the last word"), ("delete_words", "1"))
check("delete counted", voice.parse_edit("delete the last 5 words"), ("delete_words", "5"))
check("delete sentence", voice.parse_edit("remove the last sentence"), ("delete_sentence", ""))
check("edit prose is not an edit", voice.parse_edit("remove one filter"), None)

# --- punctuation ----------------------------------------------------------
check("punct comma", steps("comma"), [("punct:,", "")])
check("punct named", steps("add semicolon"), [("punct:;", "")])
check("spacing cleaned", voice._clean_spacing("Hello there ."), "Hello there.")

# --- formatters -----------------------------------------------------------
check("snake", voice.format_words("hello world", "snake"), "hello_world")
check("camel", voice.format_words("hello world", "camel"), "helloWorld")
check("title", voice.format_words("hello world", "title"), "Hello World")

# --- deletion counts words, not tokens -----------------------------------
check("token span punctuation", voice._tokens_for_words("hello world .", 1), 2)
check("trim punctuation", voice._trim_tokens("hello world .", 2), "hello")

if FAILS:
    print("FAILED %d check(s):" % len(FAILS))
    for failure in FAILS:
        print(" -", failure)
    sys.exit(1)
print("parser tests: all checks passed")
