#pragma once
class Shape {
public:
    virtual ~Shape() = default;
    virtual double area() const;
};
class Circle : public Shape {
    double r;
public:
    Circle(double radius);
    double area() const override;
};
