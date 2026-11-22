package app

import "example.com/acme/proj/model"

// Service wires an application to a model.Store (cross-package field type -> DEPENDS_ON).
type Service struct {
	store *model.Store
}

// NewService constructs a Service backed by a fresh Store. It calls model.New across the
// package boundary (package-qualified call -> CALLS) and constructs a Service (-> DEPENDS_ON).
func NewService() *Service {
	return &Service{store: model.New()}
}

func (svc *Service) Save(value string) error {
	return svc.store.Put(value)
}

func (svc *Service) Count() int {
	return svc.store.Size()
}
