# 54 New IT System

This document provides instructions on how to set up and run the project locally. The project consists of a React frontend and a Python FastAPI backend.

## Prerequisites

Before you begin, ensure you have the following installed on your system:

- **Git:** For cloning the repository.
- **Node.js and npm:** For managing the frontend dependencies and running the development server. You can download it from [nodejs.org](https://nodejs.org/).
  - To check your versions, run:
    ```bash
    node -v
    npm -v
    ```
- **Python 3:** For running the backend server. You can download it from [python.org](https://www.python.org/).
  - To check your version, run:
    ```bash
    python --version
    # or on some systems
    python3 --version
    ```

## Setup and Installation

Follow these steps to get your development environment set up.

### 1. Clone the Repository

First, clone the project repository to your local machine using Git.

```bash
git clone <your-repository-url>
cd "54 New IT System"
```
Replace `<your-repository-url>` with the actual URL of your Git repository.

### 2. Backend Setup

The backend is a Python application built with the FastAPI framework.

1.  **Navigate to the backend directory:**
    ```bash
    cd backend
    ```

2.  **Create and activate a virtual environment (Recommended):**
    This helps to isolate project-specific dependencies.

    - For macOS/Linux:
      ```bash
      python3 -m venv venv
      source venv/bin/activate
      ```
    - For Windows:
      ```bash
      python -m venv venv
      .\venv\Scripts\activate
      ```

3.  **Install Python dependencies:**
    Install all the required packages listed in `requirements.txt`.
    ```bash
    pip install -r requirements.txt
    ```

4.  **Configure Environment Variables:**
    The backend requires database credentials, which are loaded from a `.env` file. Create a file named `.env` inside the `backend` directory and add the following variables. **Do not commit this file to version control.**

    ```env
    DB_HOST=your_database_host
    DB_USER=your_database_user
    DB_PASSWORD=your_database_password
    DB_NAME=your_database_name
    DB_PORT=3306
    DB_CHARSET=utf8mb4
    ```
    Replace the placeholder values with your actual MySQL database connection details.


### 3. Frontend Setup

The frontend is a React application built with Vite.

1.  **Navigate to the frontend directory:**
    From the project root directory:
    ```bash
    cd frontend
    ```

2.  **Install Node.js dependencies:**
    This command reads the `package.json` file and installs all the necessary packages.
    ```bash
    npm install
    ```

## Running the Project

You need to run both the backend and frontend servers simultaneously in separate terminal windows.

### 1. Run the Backend Server

1.  **Navigate to the backend directory:**
    ```bash
    cd backend
    ```
2.  **Activate the virtual environment** (if you are in a new terminal):
    - macOS/Linux: `source venv/bin/activate`
    - Windows: `.\venv\Scripts\activate`

3.  **Start the FastAPI server:**
    The `uvicorn` command starts the server. The `--reload` flag enables hot-reloading, which automatically restarts the server when you make changes to the code.
    ```bash
    uvicorn main:app --reload
    ```
    The backend API will be running at `http://127.0.0.1:8000`.

### 2. Run the Frontend Development Server

1.  **Navigate to the frontend directory:**
    ```bash
    cd frontend
    ```

2.  **Start the Vite development server:**
    This command starts the frontend application.
    ```bash
    npm run dev
    ```
    The frontend will be accessible in your web browser, typically at `http://localhost:5173`. The terminal will show the exact URL.
