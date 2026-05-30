"""A small sample module so the demo has something to chew on."""


def validate(item):
    """Raise if the item is invalid."""
    if item is None:
        raise ValueError("item required")
    return True


def normalize(item):
    """Lowercase and strip an item name."""
    return str(item).strip().lower()


class Repository:
    """Base persistence layer."""

    def save(self, record):
        raise NotImplementedError


class CartRepository(Repository):
    """Stores cart line items."""

    def add(self, item):
        validate(item)
        clean = normalize(item)
        return self.save(clean)

    def save(self, record):
        # pretend to persist
        return {"stored": record}


def checkout(cart_repo, items):
    """Add every item then total the cart."""
    for it in items:
        cart_repo.add(it)
    return total(items)


def total(items):
    return len(items)
