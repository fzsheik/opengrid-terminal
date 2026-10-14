"""Train a small CNN on MNIST."""
import argparse

import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader
from torchvision import datasets, transforms

from model import Net
from utils import accuracy


def main():
    parser = argparse.ArgumentParser(description="MNIST CNN")
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--epochs", type=int, default=10)
    parser.add_argument("--lr", type=float, default=0.01)
    args = parser.parse_args()

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    tf = transforms.Compose([transforms.ToTensor(), transforms.Normalize((0.1307,), (0.3081,))])
    train_ds = datasets.MNIST("./mnist", train=True, download=True, transform=tf)
    test_ds = datasets.MNIST("./mnist", train=False, transform=tf)
    train_loader = DataLoader(train_ds, batch_size=args.batch_size, shuffle=True)
    test_loader = DataLoader(test_ds, batch_size=1000)

    model = Net().to(device)
    optimizer = torch.optim.SGD(model.parameters(), lr=args.lr, momentum=0.9)
    for epoch in range(args.epochs):
        model.train()
        for x, y in train_loader:
            x, y = x.to(device), y.to(device)
            optimizer.zero_grad()
            loss = F.cross_entropy(model(x), y)
            loss.backward()
            optimizer.step()
        model.eval()
        with torch.no_grad():
            accs = [accuracy(model(x.to(device)), y.to(device)) for x, y in test_loader]
        print(f"epoch {epoch}: test acc {sum(accs) / len(accs):.4f}")
    torch.save(model.state_dict(), "mnist_cnn.pt")


if __name__ == "__main__":
    main()
