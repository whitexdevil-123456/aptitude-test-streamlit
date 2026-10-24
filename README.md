# Class Test Portal — Streamlit + Supabase

A starter web app for classroom aptitude assessments and programming tests. It includes separate student/admin login, a persistent Supabase PostgreSQL database, randomized question selection, timed attempts, answer autosaving, history, roster import, question management, and teacher grading.

## Features

- Student login with roll number + PIN. PINs are hashed with Werkzeug before storage.
- Separate admin login controlled by configuration, not a public signup form.
- Aptitude MCQs and short-answer questions with automatic answer-key grading.
- Programming/code questions with a code text area and an output text area.
- Per-attempt random selection from active questions, with the chosen question order and marks saved for the attempt.
- Deadline stored in the database, countdown in the UI, autosave callbacks, draft saving, and submission on timeout.
- Student result history and admin CSV export.
- Admin question creation/edit/deactivation, exam timing/count configuration, student creation/CSV import, and grade review.
- Database RLS enabled and no public policies created. The app uses Supabase's service-role key on the server only.

## Important programming-test limitation

This version does **not** execute submitted code. Executing arbitrary student programs on the same server as a web application is unsafe. Students submit code and their output; teachers can manually grade. If a coding question has an expected output, the app compares the submitted output with that expected output for a basic automatic score. That output match does not prove the submitted code is correct. For serious automated evaluation, add an isolated execution service/container with CPU, memory, time, filesystem, and network restrictions.

## 1. Create the Supabase database

1. Create a Supabase project at <https://supabase.com/>.
2. Open **SQL Editor** in the Supabase dashboard.
3. Paste and run the full contents of `schema.sql`.
4. In **Project Settings → API** (or the current API keys page), copy the project URL and the **service-role/secret server-side key**.
5. Do not use the public anon key for this app, and never expose the service-role key in a browser, repository, screenshot, or student-facing output. The key bypasses row-level security, which is why it is only read by the Python server.

The SQL adds two exam configurations and a few demo questions. You can remove or deactivate the sample questions from the Admin panel after logging in.

## 2. Configure local development (Windows PowerShell)

From the project folder:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .streamlit\secrets.toml.example .streamlit\secrets.toml
```

Edit `.streamlit/secrets.toml` and set `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, and `ADMIN_USERNAME`.

Generate a password hash in the activated virtual environment:

```powershell
python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('ReplaceWithAStrongPassword!'))"
```

Copy the hash printed by the command into `ADMIN_PASSWORD_HASH` in `.streamlit/secrets.toml`. Choose your own password; do not use the example literally.

Run the app:

```powershell
streamlit run app.py
```

Open the local URL Streamlit prints (normally `http://localhost:8501`). Log in as the admin, open **Students**, and create a student or import a roster CSV. `sample_students.csv` is only an example; change the sample PINs before using real student data.

## 3. Deploy to PythonAnywhere

**Important:** PythonAnywhere's documented Streamlit option is currently experimental and requires a paid account (at least the Basic Hacker tier in the cited help page). Confirm the current account requirements, availability, and pricing before committing to this route. The CLI and deployment API may change. Official guide: <https://help.pythonanywhere.com/pages/Streamlit/>

### Prepare the project

1. Create a PythonAnywhere account that meets the current requirements.
2. Upload the project files (or clone a private Git repository) into `/home/YOURUSERNAME/aptitude_programming_portal/`.
3. Open a Bash console on PythonAnywhere and run commands similar to the following, replacing `YOURUSERNAME` with your real PythonAnywhere username:

```bash
cd /home/YOURUSERNAME/aptitude_programming_portal
mkvirtualenv aptitude-portal --python=python3.13
workon aptitude-portal
pip install -r requirements.txt
pip install --upgrade pythonanywhere
```

If Python 3.13 is not offered in your account, select a Python version supported by PythonAnywhere and your dependency versions.

4. Create `/home/YOURUSERNAME/aptitude_programming_portal/.streamlit/secrets.toml` using the same entries as the example file. Fill in the actual Supabase URL, service-role key, admin username, and password hash. Keep that file out of Git.
5. Generate an API token using PythonAnywhere's account/API instructions. Its current Streamlit guide documents use of the `pa` command-line tool.
6. Create the Streamlit site using the command pattern from the official guide. Replace every username/path with yours and preserve the server arguments:

```bash
pa website create --domain YOURUSERNAME.pythonanywhere.com --command '/home/YOURUSERNAME/.virtualenvs/aptitude-portal/bin/streamlit run /home/YOURUSERNAME/aptitude_programming_portal/app.py --server.address "unix://${DOMAIN_SOCKET}" --server.enableCORS false --server.enableXsrfProtection false --server.enableWebsocketCompression false'
```

PythonAnywhere's guide uses `--server.enableWebsocketCompression false`; if your installed Streamlit version rejects it, inspect `streamlit run --help` and use the supported equivalent.

7. Open `https://YOURUSERNAME.pythonanywhere.com`, then test student login, a timed attempt, the admin panel, and result export.
8. Review PythonAnywhere's deployment output/logs if the site does not start. CLI syntax and the web interface are experimental and may change.

### Deployment caveats

- PythonAnywhere's Streamlit deployment is experimental; availability and account requirements can change.
- Confirm that your PythonAnywhere account can reach your Supabase project's HTTPS API endpoint.
- Supabase Free projects may pause after one week of inactivity; resume the project before a scheduled exam if necessary.
- Perform a small pilot with test accounts before an exam. Do not use this starter as a high-stakes examination platform without security review, backup/export procedures, and load testing.

## Alternative: free Streamlit hosting

If your priority is a **$0 hosting bill**, use [Streamlit Community Cloud](https://docs.streamlit.io/deploy/streamlit-community-cloud) with the same Supabase database. Streamlit describes Community Cloud as a free hosting option and supports adding secrets from the app settings. The app code does not need a hosting-specific rewrite. You will need to push this project to a GitHub repository, deploy `app.py`, and add the three Supabase/admin settings in the Community Cloud secrets panel. Keep `.streamlit/secrets.toml` out of Git.

This option is simpler than PythonAnywhere's experimental Streamlit workflow for a student project. It is still best to run a pilot and verify its current resource limits and availability before a scheduled test.

## 4. Student roster CSV

The CSV must use these exact column headers:

```csv
roll_number,full_name,pin
DS001,Student Name,482913
DS002,Another Student,725164
```

Keep `pin` as text so any leading zeros are retained. The PIN should be at least four characters. Upload it from Admin → Students. Importing a roll number already in the database changes that student's name and PIN hash.

## 5. Database tables

- `students`: roll number, full name, hashed PIN, active flag.
- `questions`: aptitude/programming question bank, options, answer key, expected output, marks, active flag.
- `exam_settings`: duration, number of questions, and whether each exam is enabled.
- `attempts`: student, section, status, start/deadline/submission times, score.
- `attempt_questions`: frozen order and mark value for every question in a particular attempt.
- `answers`: saved text, code, output, and awarded marks.

## 6. Known MVP limits / recommended next improvements

1. Add email or institutional SSO, stronger login throttling, audit logging, and teacher accounts with roles if this grows beyond a small class.
2. Add question CSV import and a richer question editor, including images/code formatting.
3. Add explicit exam windows, attempt limits, negative marking, and review rules if needed.
4. Add secure isolated execution for programming questions only after choosing an execution sandbox service.
5. Configure database backups and a retention policy before storing real student records.
6. For a high-stakes exam, use a conventional production framework or managed assessment platform rather than relying on experimental Streamlit hosting.

## Troubleshooting

- **Missing Supabase configuration:** check `.streamlit/secrets.toml` exists beside `app.py` and all key names match the example.
- **Invalid admin login:** regenerate `ADMIN_PASSWORD_HASH` using the command above; the config expects a Werkzeug password hash, not plain text.
- **No questions available:** make sure `schema.sql` has run and the relevant questions are active in the Admin question bank.
- **Answers do not save:** verify internet connectivity from the hosted app and the Supabase URL/key; check app logs. The service-role key is required for this server-side design.
- **Past score changed after editing questions:** attempt marks are snapshotted in `attempt_questions`; changes to question-bank marks should apply to new attempts, not past ones.
