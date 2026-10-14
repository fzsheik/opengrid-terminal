import os

from flask import Flask, jsonify, request

from models import Todo, db

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL", "sqlite:///todos.db")
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY")
db.init_app(app)


@app.get("/todos")
def list_todos():
    return jsonify([t.to_dict() for t in Todo.query.order_by(Todo.id).all()])


@app.post("/todos")
def add_todo():
    todo = Todo(title=request.json["title"])
    db.session.add(todo)
    db.session.commit()
    return jsonify(todo.to_dict()), 201


if __name__ == "__main__":
    app.run(debug=True)
