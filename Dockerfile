FROM python:3.10-slim

COPY ./app /app
COPY requirements.txt /app

WORKDIR /app

RUN mkdir /app/uploads
RUN mkdir /app/database

RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 8080

# Define environment variable
ENV PYTHONUNBUFFERED=1

# Run app.py when the container launches
CMD ["python", "app.py"]
