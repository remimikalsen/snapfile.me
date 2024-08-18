FROM python:3.12-slim


COPY ./app /app
COPY requirements.txt /app

WORKDIR /app

RUN mkdir /app/uploads
RUN mkdir /app/database

RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 8080

# Define environment variable
ENV PYTHONUNBUFFERED=1

# Needed for bandwith control
# This also requires the container to be run with the --cap-add=NET_ADMIN flag
# It will not work on a non-linux host
# You may need to run 'sudo modprobe ifb' on the host
RUN apt-get update && apt-get install -y iproute2
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]

# Run app.py when the container launches
CMD ["python", "app.py"]
