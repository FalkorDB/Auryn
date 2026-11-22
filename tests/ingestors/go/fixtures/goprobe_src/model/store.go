package model

import "errors"

// MaxItems caps how many values a Store holds.
const MaxItems = 100

const (
	Prefix  = "item-"
	Version = 2
)

// Base carries a shared identifier, meant for embedding.
type Base struct {
	id ID
}

// Store is an in-memory Repository. It embeds Base (struct embedding -> EMBEDS).
type Store struct {
	Base
	items []string
}

// New builds an empty Store (struct-literal construction -> DEPENDS_ON Store).
func New() *Store {
	return &Store{}
}

func (s *Store) Put(value string) error {
	if len(s.items) >= MaxItems {
		return errors.New("store full")
	}
	s.record(value)
	return nil
}

func (s *Store) record(value string) {
	s.items = append(s.items, value)
}

func (s *Store) Size() int {
	return len(s.items)
}

// Merge copies every value from other into s.
func (s *Store) Merge(other *Store) {
	for _, v := range other.items {
		s.record(v)
	}
}

// Read returns the store's item prefix.
func (s Store) Read() string {
	return Prefix
}
