# Write or Perish

Write or Perish is a web-based digital journal designed to archive personal thoughts and writings into a public repository. The collected writings may also serve as training data for future AI models. Users write long-form content organized as “nodes” in a tree structure. The app supports Twitter OAuth login, dynamic node creation (including LLM responses), and comprehensive user/global statistics.

## Repository Structure

This is a monorepo containing both backend and frontend code:

```
write-or-perish/
├── backend/
│   ├── __init__.py             # Application factory
│   ├── app.py                  # Entry point (runs the app)
│   ├── config.py               # App configuration
│   ├── extensions.py           # Flask extensions (e.g., SQLAlchemy instance)
│   ├── models.py               # Database models
│   └── routes/                 # API routes
│       ├── __init__.py
│       ├── auth.py
│       ├── nodes.py
│       ├── dashboard.py
│       └── export_data.py
└── frontend/                   # Your React code will reside here
```

## Getting Started

### Prerequisites

Before starting, ensure you have the following installed:
- Python 3.9+  
- Conda (Anaconda or Miniconda)
- PostgreSQL (ensure the server is installed and running)
- Git

### 1. Clone the Repository

Clone this repository to your local machine:

```
git clone git@github.com:hrosspet/write-or-perish.git
cd write-or-perish
```

### 2. Create and Activate the Conda Environment

Create a new conda environment called `write-or-perish` with Python 3.9 (or your preferred version):

```
conda create -n write-or-perish python=3.9
conda activate write-or-perish
```

### 3. Install Python Dependencies

Navigate to the backend folder and install dependencies via pip (the dependencies are listed in `backend/requirements.txt`):

```
cd backend
pip install -r requirements.txt
```

> Note: Even though you’re using conda, use pip inside your active conda environment.

### 4. Set Up Environment Variables

Create a `.env` file in the `backend/` directory with the required environment variables:

```
SECRET_KEY=your-secret-key
DATABASE_URL=postgresql://username:password@localhost/writeorperish
TWITTER_API_KEY=your-twitter-api-key
TWITTER_API_SECRET=your-twitter-api-secret
OPENAI_API_KEY=your-openai-api-key
```

Make sure your PostgreSQL database exists and the credentials match. If needed, create the database (see note below).

#### Note on PostgreSQL:
If you experience issues using the `createdb` command, ensure PostgreSQL’s binaries are installed and on your PATH. After that, create your database using either:
```
createdb writeorperish
```
or via `psql`:
```
psql -c "CREATE DATABASE writeorperish;"
```

### 5. Database Migration

Initialize and run the database migrations with Flask-Migrate. From the `backend/` folder, run:

```
flask db init
flask db migrate -m "Initial migration."
flask db upgrade
```

### 6. Running the Backend Server

From the repository root (the folder containing both `backend/` and `frontend/`), run the application using the module flag:

```
python -m backend.app
```

This command uses the application factory defined in `backend/__init__.py` and the separate database instance in `backend/extensions.py`. Your server should start and be accessible at http://localhost:5000.

### 7. Frontend Setup

Place your React code within the `frontend/` folder. The frontend can communicate with the backend API endpoints (e.g., `/api/nodes`, `/auth/login`) as required.

## Additional Information

- **Application Factory & Extensions:**
  - The app uses an application factory pattern (`backend/__init__.py`) which initializes the Flask app and its extensions.
  - Flask-SQLAlchemy is configured in a separate module (`backend/extensions.py`) to avoid circular import errors. Models in `backend/models.py` import the database instance from here.
  
- **OAuth with Twitter:**
  - Authentication is managed via Twitter OAuth (using Flask-Dance). Users log in using their Twitter credentials.

- **LLM Integration:**
  - The backend calls OpenAI’s API (using the model “gpt-4.5-preview”) for generating LLM responses based on nodes’ text threads.
  
- **Data Privacy & Onboarding:**
  - The app is designed as a public archive. Ensure users are informed about data publicness and privacy policies, and that no protected health or sensitive information is written.
  
- **Usage Statistics:**
  - The app tracks tokens generated from LLM interactions, providing both personal and global statistics.

- **Data Export & Delete:**
  - Users can export all their app data (as JSON) or delete it. Note: deletion only removes data from the app’s database, not from external services like OpenAI.

## Contributing

Feel free to fork this repository and submit pull requests. For major changes, please open an issue describing the proposed change.

## License

[Insert your license information here.]
