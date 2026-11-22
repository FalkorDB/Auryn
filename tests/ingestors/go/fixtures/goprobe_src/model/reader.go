package model

// Reader is the read side of a repository.
type Reader interface {
	Read() string
}

// ID aliases the identifier type used across the model package.
type ID = int
