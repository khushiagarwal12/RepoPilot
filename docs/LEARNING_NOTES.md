# Learning notes
- Two datastores cannot be atomic together: build the new vectors first, commit the pointer last, clean up afterwards.
- A passing test can encode a bug (the old symlink test); use mutation checks to prove tests can fail.
- Libraries can read files you did not expect (Chroma reads `./.env`); pin behaviour explicitly.
- Enforce invariants in the database (partial unique index), not only in application code.
