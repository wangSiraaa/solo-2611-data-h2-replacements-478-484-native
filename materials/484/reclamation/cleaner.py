class Cleaner:
    def __init__(self, zones, index):
        self.zones = zones
        self.index = index
        self.events = []

    def clean(self, zone, after_copy=None, before_reset=None):
        target = (zone + 1) % self.zones.count
        moved = 0
        for key, (version, location) in self.index.entries():
            if location[0] != zone:
                continue
            data = self.zones.read(location)
            new_location = self.zones.append(target, data)
            if after_copy:
                after_copy(key, version, location, new_location)
            accepted = self.index.relocate(key, version, location, new_location)
            self.events.append(('relocate', key, version, accepted))
            moved += len(data)
        if before_reset:
            before_reset(zone)
        if self.index.references(zone):
            raise RuntimeError('busy zone')
        self.zones.reset(zone)
        return {'zone': zone, 'moved_bytes': moved}
