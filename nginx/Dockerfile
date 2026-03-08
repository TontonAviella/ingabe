FROM nginx:1.27-alpine

# Replace default nginx.conf with our performance-tuned version
COPY main.conf /etc/nginx/nginx.conf

RUN rm /etc/nginx/conf.d/default.conf
COPY nginx.conf /etc/nginx/conf.d/mundi.conf
