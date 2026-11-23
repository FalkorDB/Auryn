package model

// Repository stores string values and can be read from. It embeds Reader, so a Repository
// is also a Reader (interface embedding -> EMBEDS).
type Repository interface {
	Reader
	Put(value string) error
	Size() int
}
