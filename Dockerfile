# Use official Python image as base
FROM python:3.11

# Set the working directory
WORKDIR /app

# Copy the requirements file
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the entire project
COPY . .

# Expose the FastAPI port
EXPOSE 8000

# Run the application
CMD ["uvicorn", "app.main:esim_app", "--host", "0.0.0.0", "--port", "8000"]
