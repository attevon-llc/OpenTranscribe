# Bundled common-password blocklist

`common_passwords.txt` is the offline blocklist used by the password policy
(`app/auth/password_blocklist.py`). One password per line, most common first,
case-sensitive as published (matching is case-insensitive).

- **Content:** the 100,000 most-used passwords from the UK National Cyber Security
  Centre (NCSC) "PwnedPasswordsTop100k" analysis of the public Have I Been Pwned
  corpus. 99,840 entries.
- **Obtained from:** `Passwords/Common-Credentials/100k-most-used-passwords-NCSC.txt`
  in https://github.com/danielmiessler/SecLists (MIT License, Copyright (c) 2018
  Daniel Miessler). The file is committed unmodified.
- **Licence:** distributed here under the SecLists MIT License (full text below). The
  upstream NCSC publication is released under the UK Open Government Licence v3.0
  (https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/). The list
  contains passwords only - no usernames, emails or other personal data.
- **Replacing / extending it:** point `PASSWORD_BLOCKLIST_PATH` at your own UTF-8 file
  (one entry per line). It *replaces* this one; to extend, `cat` this file plus your
  additions into a new file.

## SecLists MIT License

Copyright (c) 2018 Daniel Miessler

Permission is hereby granted, free of charge, to any person obtaining a copy of this
software and associated documentation files (the "Software"), to deal in the Software
without restriction, including without limitation the rights to use, copy, modify,
merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
permit persons to whom the Software is furnished to do so, subject to the following
conditions:

The above copyright notice and this permission notice shall be included in all copies
or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A
PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF
CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR
THE USE OR OTHER DEALINGS IN THE SOFTWARE.
