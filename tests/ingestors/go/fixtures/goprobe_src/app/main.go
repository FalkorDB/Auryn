package app

import "fmt"

// Run wires up a Service and exercises it. The bare NewService() call resolves within the
// app package (-> CALLS Run -> NewService).
func Run() {
	svc := NewService()
	if err := svc.Save("hello"); err != nil {
		fmt.Println(err)
	}
	fmt.Println("count", svc.Count())
}
