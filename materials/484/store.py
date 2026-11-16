class Store:
    def __init__(self, zones, index, cleaner):
        self.zones = zones
        self.index = index
        self.cleaner = cleaner
        self.active = 0

    def put(self, key, data):
        try:
            location = self.zones.append(self.active, data)
        except BufferError:
            self.active = (self.active + 1) % self.zones.count
            self.zones.reset(self.active)
            location = self.zones.append(self.active, data)
        return self.index.publish(key, location)

    def get(self, key, after_pin=None):
        handle = self.index.pin(key)
        try:
            if after_pin:
                after_pin(handle)
            return self.zones.read(handle.location)
        finally:
            self.index.release(handle)

    def clean(self, zone, **hooks):
        return self.cleaner.clean(zone, **hooks)
